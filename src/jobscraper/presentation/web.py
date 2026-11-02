import json
import logging
import shlex
import threading
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlencode, urlparse

import httpx

from jobscraper import activity, adapters
from jobscraper import config as config_file
from jobscraper.classification import classify, jev_triage, salaries, usage
from jobscraper.config import Config
from jobscraper.geography import distance, locations
from jobscraper.presentation import onboarding, tracked
from jobscraper.scraping import crawl, discovery, http
from jobscraper.storage import company_store, employers, jsonfile, state, userdata
from jobscraper.storage.state import Posting, State
from jobscraper.timezone import EASTERN
from jobscraper.workflow import notify
from jobscraper.workflow.tracker import FAILURES_BEFORE_ALERT

log = logging.getLogger(__name__)

# "New" badge colors by when a posting was first seen: red if the latest check found it,
# then orange up to NEW_HOURS, yellow up to RECENT_HOURS, and no badge after that.
NEW_HOURS = 48
RECENT_HOURS = 96
# Last check before the colored badges started; postings already loaded by then get none.
BADGES_SINCE = datetime(2026, 10, 13, 17, 37, 26, 111779, tzinfo=UTC)
STALE_HOURS = 6  # no successful check for this long means the runner (or my PC) is probably off
# Matches deploy/jobscraper-crawl.timer (US Eastern); a test keeps them in sync.
SCHEDULE_MINUTE = 0
SCHEDULE_EVERY_HOURS = 2

VIEWS = {
    "exact": "Exact",
    "near": "Close",
    "maybe": "Probably not",
    "industrial": "Industrial / controls",
    "relevant": "All matches",
    "unverified": "Unverified",
    "starred": "Starred",
    "applied": "Applied",
    "hidden": "Hidden",
    "blocked": "Blocked",
    "companies": "Companies",
}
SOURCES = {"all": "All sources", "companies": "Company sites", "boards": "Job boards"}
SORTS = {
    "newest": "Newest",
    "oldest": "Oldest",
    "relevant": "Most relevant",
    "least_relevant": "Least relevant",
    "nearest": "Nearest",
    "farthest": "Farthest",
    "salary": "Highest salary",
    "salary_lowest": "Lowest salary",
}
REVERSE_SORT = dict(
    zip(
        SORTS,
        (
            "oldest",
            "newest",
            "least_relevant",
            "relevant",
            "farthest",
            "nearest",
            "salary_lowest",
            "salary",
        ),
        strict=True,
    )
)
MOVE_DESTINATIONS = frozenset(
    ("exact", "near", "maybe", "industrial", "starred", "applied", "hidden")
)
EMPLOYER_ACTIONS = {"approve": "approved", "block": "blocked", "clear": None}


@dataclass
class Row:
    posting: Posting
    group: str | None
    marks: dict
    source: str  # display label, e.g. "AFL (via Move Upstate SC)"
    is_board: bool
    employer_status: str  # "trusted", "ok", "approved", "unverified", or "blocked"
    employer_key: str
    # Where else this job is listed, as (board name, url).
    also_on: list[tuple[str, str]] = field(default_factory=list)
    miles: float | None = None  # from [web] home; None for remote or unknown places
    jev: dict | None = None  # Jev triage record, on the /jev page


class App:
    def __init__(
        self,
        cfg: Config,
        state_dir: Path,
        marks_path: Path,
        employers_path: Path,
        config_path: Path | None = None,
    ):
        self.state_dir = state_dir
        self.marks_path = marks_path
        self.employers_path = employers_path
        self.companies_path = marks_path.parent / "companies.json"
        self.jev_dir = marks_path.parent / "jev"
        self._triage: dict[str, tuple[int, dict]] = {}
        self.discovery_lock = threading.Lock()
        self._base_cfg = cfg
        self.config_path = config_path  # reloaded when it changes, if given
        self._config_mtime = config_path.stat().st_mtime if config_path else None
        self.lock = threading.Lock()
        self._cache: tuple[tuple[int, ...], State] | None = None
        self._use_config(cfg)

    def _use_config(self, cfg: Config) -> None:
        cfg = company_store.merge(cfg, self.companies_path)
        self.cfg = cfg
        self.scopes = locations.scopes(cfg)
        self.companies = {c.id: c for c in cfg.companies}
        self.home = distance.home_coords(cfg.web.home)

    def reload_config(self) -> None:
        """Reload the published config and companies added through the page."""
        if self.config_path and self.config_path.exists():
            mtime = self.config_path.stat().st_mtime
            if mtime != self._config_mtime:
                try:
                    self._base_cfg = config_file.load(self.config_path)
                except Exception:
                    log.exception(
                        "Couldn't reload %s; keeping the previous config", self.config_path
                    )
                self._config_mtime = mtime
        self._use_config(self._base_cfg)

    def rename_company(self, body: dict) -> dict:
        company_id = body.get("company_id")
        if not isinstance(company_id, str):
            raise ValueError("Company id must be text.")
        with self.lock:
            self.reload_config()
            if company_id not in self.companies:
                raise KeyError(company_id)
            company_store.rename(self.companies_path, company_id, body.get("name"))
            self.reload_config()
            return {"company_id": company_id, "name": self.companies[company_id].name}

    def add_company(self, body: dict) -> dict:
        # Keep discovery serial so repeated clicks don't launch overlapping crawls.
        if not self.discovery_lock.acquire(blocking=False):
            raise ValueError("Another company is being checked. Please wait for it to finish.")
        try:
            with activity.Trace(
                self.marks_path.parent / "discovery-history.json",
                "add_company",
                url=body.get("url", ""),
            ) as trace:
                try:
                    result = self._add_company(body)
                except Exception as exc:
                    exc.add_note("Log: /logs?id=" + trace.record["id"])
                    raise
                trace.record["status"] = result["status"]
                activity.emit(
                    "outcome",
                    status=result["status"],
                    message=result["message"],
                    help=result.get("help", []),
                )
                return result | {"log_url": "/logs?id=" + trace.record["id"]}
        finally:
            self.discovery_lock.release()

    def _add_company(self, body: dict) -> dict:
        self.reload_config()
        is_board = body.get("is_board", False)
        if not isinstance(is_board, bool):
            raise ValueError("is_board must be true or false.")
        url = crawl.normalize_url(body.get("url", ""))
        if any(c.url.rstrip("/") == url.rstrip("/") for c in self.cfg.companies):
            raise ValueError("This careers page is already being tracked.")
        region = body.get("locations", "")
        entries = (region,) if region else ()
        location_filter = locations.build_filter(entries, self.cfg.regions)
        scope_states = location_filter.watched_states
        if entries and self.cfg.extended_locations:
            scope_states |= locations.build_filter(
                self.cfg.extended_locations, self.cfg.regions
            ).watched_states
        with http.client() as client:
            result = discovery.inspect(
                url,
                crawl.Client(client, limit=200),
                name=body.get("name", ""),
                settings=body.get("settings"),
                states=tuple(sorted(scope_states)) if entries else (),
            )
        if result.company is None:
            return {"status": result.status, "message": result.message, "help": result.help}
        company = replace(result.company, locations=entries, is_board=is_board)
        with self.lock:
            self.reload_config()
            if any(
                c.id == company.id or c.url.rstrip("/") == company.url.rstrip("/")
                for c in self.cfg.companies
            ):
                raise ValueError("This jobs source is already being tracked.")
            activity.emit("saving_company", company=company.id, url=company.url)
            company_store.add(self.companies_path, company)
            activity.emit("company_saved", company=company.id)
            self.reload_config()
        return {
            "status": "added",
            "company_id": company.id,
            "message": result.message + " Tracking is enabled for the next scheduled check.",
            "sample": [{"title": p.title, "location": p.location} for p in result.postings[:5]],
        }

    # --- data ---

    def state(self) -> State:
        """Reload only when the runner has written new state."""
        paths = [self.state_dir / name for name in ("postings.json", "health.json")]
        mtime = tuple(path.stat().st_mtime_ns if path.exists() else 0 for path in paths)
        if self._cache is None or self._cache[0] != mtime:
            self._cache = (mtime, state.load(self.state_dir))
        return self._cache[1]

    def triage(self, version: str) -> dict:
        path = jev_triage.store_path(self.jev_dir, version)
        mtime = path.stat().st_mtime_ns if path.exists() else 0
        if version not in self._triage or self._triage[version][0] != mtime:
            self._triage[version] = (mtime, jsonfile.read(path))
        return self._triage[version][1]

    def rows(self, evaluator: str = "sonnet", version: str = jev_triage.LATEST) -> list[Row]:
        st = self.state()
        triage = self.triage(version) if evaluator == "jev" else {}
        description_cache = (
            jsonfile.read(self.state_dir / "descriptions.json") if evaluator == "jev" else {}
        )
        marks = jsonfile.read(self.marks_path)
        decisions = jsonfile.read(self.employers_path)
        duplicates: dict[str, list[tuple[str, str]]] = {}
        for p in st.postings.values():
            if p.duplicate_of and p.status == "active":
                board = self.companies.get(p.company_id)
                name = board.name if board else p.company_id
                duplicates.setdefault(p.duplicate_of, []).append((name, p.url))
        out = []
        for p in st.postings.values():
            company = self.companies.get(p.company_id)
            jev_record = triage.get(p.key)
            if (
                evaluator == "jev"
                and jev_record
                and (
                    company is None
                    or jev_record.get("signature")
                    != jev_triage.signature(
                        jev_triage.request_body(
                            p, self.cfg, version, description=description_cache.get(p.key)
                        )
                    )
                )
            ):
                jev_record = None
            personal = marks.get(p.key, {})
            scope = self.scopes.get(p.company_id)
            group = classify.posting_group(p, self.cfg.profile, scope)
            if evaluator == "jev":
                # Moves made on the main page are judgments of Sonnet's tabs, so they don't apply.
                group = jev_triage.posting_group(p, jev_record, scope, version)
            elif (
                personal.get("group")
                and not p.duplicate_of
                and (scope is None or scope.area(p) is not None)
            ):
                group = personal["group"]
            out.append(
                Row(
                    posting=p,
                    group=group,
                    marks=marks.get(p.key, {}),
                    source=p.source_label(company.name if company else p.company_id),
                    is_board=employers.is_board(p, self.companies),
                    employer_status=employers.status(p, self.companies, decisions),
                    employer_key=employers.employer_key(p),
                    also_on=duplicates.get(p.key, []),
                    miles=distance.miles_from(self.home, p.location),
                    jev=jev_record,
                )
            )
        return out

    def decide_employer(self, key: str, action: str) -> None:
        """Approve, block, or clear (back to Claude's call) one job-board employer."""
        if action not in EMPLOYER_ACTIONS:
            raise ValueError(f"Unknown action {action!r}")
        postings = [
            p
            for p in self.state().postings.values()
            if employers.is_board(p, self.companies) and employers.employer_key(p) == key
        ]
        if not postings:
            raise KeyError(key)
        with self.lock:
            decisions = jsonfile.read(self.employers_path)
            name = postings[0].employer or postings[0].title
            employers.decide(decisions, key, name, EMPLOYER_ACTIONS[action])
            jsonfile.write(self.employers_path, decisions)

    def mark(self, key: str, field: str, value) -> dict:
        if key not in self.state().postings:
            raise KeyError(key)
        with self.lock:
            marks = jsonfile.read(self.marks_path)
            entry = userdata.update(marks, key, field, value)
            jsonfile.write(self.marks_path, marks)
        return entry

    def move(self, keys: list[str], destination: str) -> int:
        """Save a whole selection atomically; a bad key never leaves a partial move."""
        if (
            not isinstance(keys, list)
            or not 1 <= len(keys) <= 500
            or any(not isinstance(k, str) for k in keys)
        ):
            raise ValueError("Select between 1 and 500 postings")
        if destination not in MOVE_DESTINATIONS:
            raise ValueError(
                "Choose a match tab, Industrial / controls, Starred, Applied, or Hidden"
            )
        self.reload_config()
        keys = list(dict.fromkeys(keys))
        rows = {r.posting.key: r for r in self.rows()}
        for key in keys:
            if key not in rows:
                raise KeyError(key)
            if destination in ("exact", "near", "maybe", "industrial"):
                row = rows[key]
                p = row.posting
                scope = self.scopes.get(p.company_id)
                if (
                    p.status != "active"
                    or p.duplicate_of
                    or (scope and scope.area(p) is None)
                    or row.employer_status in ("blocked", "unverified")
                ):
                    raise ValueError(
                        "Some selected jobs are closed, outside your area, duplicated, "
                        "or need employer approval. They cannot move to a match tab."
                    )
        with self.lock:
            marks = jsonfile.read(self.marks_path)
            for key in keys:
                if destination in ("exact", "near", "maybe", "industrial"):
                    userdata.update(marks, key, "group", destination)
                    for field in ("hidden", "starred", "applied"):
                        userdata.update(marks, key, field, False)
                else:
                    if destination != "hidden":
                        userdata.update(marks, key, "hidden", False)
                    userdata.update(marks, key, destination, True)
            jsonfile.write(self.marks_path, marks)
        return len(keys)

    # --- filtering ---

    @staticmethod
    def in_view(r: Row, view: str) -> bool:
        active = r.posting.status == "active"
        hidden = r.marks.get("hidden", False)
        applied = r.marks.get("applied", False)
        starred = r.marks.get("starred", False)
        if view == "companies":
            return False
        if view == "hidden":
            return hidden
        if view == "starred":
            return starred and not hidden
        if view == "applied":
            return applied
        # Applied jobs move to the Applied tab, like hidden ones move to Hidden.
        if hidden or applied or not active or r.group is None:
            return False
        if view == "blocked":
            return r.employer_status == "blocked"
        if view == "unverified":
            return r.employer_status == "unverified"
        # Starred jobs move to the Starred tab instead of the match tabs.
        if starred or r.employer_status in ("unverified", "blocked"):
            return False
        return (view == "relevant" and r.group != "industrial") or r.group == view

    @staticmethod
    def in_source(r: Row, source: str) -> bool:
        return source == "all" or (source == "boards") == r.is_board

    @staticmethod
    def matches_query(r: Row, q: str) -> bool:
        if not q:
            return True
        hay = f"{r.posting.title} {r.source} {r.posting.location}".lower()
        return matches_text(hay, q)

    @staticmethod
    def sort_key(r: Row, sort: str = "newest"):
        p = r.posting
        # Newest means newest to me: job boards can pick up a posting days after it went up.
        added = -p.first_seen.timestamp()
        posted = -(p.posted_date.toordinal() if p.posted_date else 0)
        title = p.title.casefold()
        if sort in ("relevant", "least_relevant"):
            score = (p.classification or {}).get("score")
            return (
                score is None,
                (score or 0) * (1 if sort == "least_relevant" else -1),
                added,
                title,
            )
        if sort in ("nearest", "farthest"):
            return (
                r.miles is None,
                (r.miles or 0) * (-1 if sort == "farthest" else 1),
                added,
                title,
            )
        if sort in ("salary", "salary_lowest"):
            salary = (
                salaries.annual_floor(p)
                if (
                    r.group in ("exact", "near", "maybe", "evaluate")
                    and not r.marks.get("hidden")
                    and r.employer_status != "blocked"
                )
                else None
            )
            return (
                salary is None,
                (salary or 0) * (1 if sort == "salary_lowest" else -1),
                added,
                title,
            )
        return (-added if sort == "oldest" else added, posted, title)

    def current_company_job(self, r: Row) -> bool:
        p = r.posting
        scope = self.scopes.get(p.company_id)
        return (
            p.status == "active"
            and r.group is not None
            and not r.marks.get("hidden")
            and r.employer_status != "blocked"
            and not p.duplicate_of
            and (scope is None or scope.area(p) is not None)
        )

    def directory_companies(self, source: str, q: str, rows: list[Row]):
        names = self.cfg.health_names()
        return [
            c
            for c in self.cfg.companies
            if (source == "all" or (source == "boards") == adapters.is_board(c))
            and (
                not q
                or matches_text(names[c.id], q)
                or any(
                    r.posting.company_id == c.id
                    and self.current_company_job(r)
                    and self.matches_query(r, q)
                    for r in rows
                )
            )
        ]

    # --- pages ---

    def select(self, query: dict[str, str]) -> tuple[list[Row], dict[str, int]]:
        """Rows in the selected tab and counts for every tab, using the same filters."""
        self.reload_config()
        views = VIEWS
        view = query.get("view") if query.get("view") in views else "exact"
        source = query.get("source") if query.get("source") in SOURCES else "all"
        q = query.get("q", "").strip()
        scoped = [r for r in self.rows() if self.in_source(r, source) and self.matches_query(r, q)]
        counts = {v: sum(self.in_view(r, v) for r in scoped) for v in views}
        counts["companies"] = len(self.directory_companies(source, q, scoped))
        if view == "companies":
            return [
                r
                for r in scoped
                if self.current_company_job(r) and r.posting.company_id == query.get("company")
            ], counts
        return [r for r in scoped if self.in_view(r, view)], counts

    def index(self, query: dict[str, str], evaluator: str = "sonnet") -> str:
        if evaluator == "jev":
            from jobscraper.presentation import jev_v6

            self.reload_config()
            return jev_v6.page(self, query)
        views = VIEWS
        view = query.get("view") if query.get("view") in views else "exact"
        source = query.get("source") if query.get("source") in SOURCES else "all"
        sort = query.get("sort") if query.get("sort") in SORTS else "newest"
        q = query.get("q", "").strip()
        shown, counts = self.select(query)
        shown.sort(key=lambda r: self.sort_key(r, sort))
        return render_page(
            self,
            view,
            source,
            q,
            sort,
            counts,
            shown,
            query.get("company", ""),
        )


def matches_text(hay: str, query: str) -> bool:
    try:
        terms = shlex.split(query.casefold())
    except ValueError:
        terms = query.casefold().split()
    for term in terms:
        if term.startswith("!") and len(term) > 1:
            if term[1:] in hay.casefold():
                return False
        elif term != "!" and term not in hay.casefold():
            return False
    return True


# --- rendering ---

# Google Fonts offered in the settings panel, by fallback category.
FONTS = {
    "sans": [
        "Inter",
        "Roboto",
        "Open Sans",
        "Lato",
        "Poppins",
        "Montserrat",
        "Nunito",
        "Source Sans 3",
        "Noto Sans",
        "IBM Plex Sans",
        "Work Sans",
        "Raleway",
        "Ubuntu",
        "Fira Sans",
        "DM Sans",
        "Manrope",
        "Rubik",
        "Outfit",
    ],
    "serif": ["Merriweather", "Lora", "Source Serif 4", "Playfair Display", "Libre Baskerville"],
    "mono": [
        "JetBrains Mono",
        "Fira Code",
        "IBM Plex Mono",
        "Roboto Mono",
        "Source Code Pro",
        "Ubuntu Mono",
        "Space Mono",
    ],
}
FONT_GROUP_LABELS = {"sans": "Sans-serif", "serif": "Serif", "mono": "Monospace"}


def _font_select(slot: str, label: str) -> str:
    groups = "".join(
        f'<optgroup label="{FONT_GROUP_LABELS[cat]}">'
        + "".join(
            f'<option value="{escape(n)}" data-cat="{cat}">{escape(n)}</option>' for n in names
        )
        + "</optgroup>"
        for cat, names in FONTS.items()
    )
    return (
        f'<label>{label}<select data-slot="{slot}">'
        f'<option value="">System default</option>{groups}</select></label>'
    )


def _link(
    view: str,
    source: str,
    q: str,
    sort: str,
    company: str = "",
) -> str:
    params = {"view": view, "source": source, "sort": sort}
    if company:
        params["company"] = company
    if q:
        params["q"] = q
    return "/?" + urlencode(params)


def _last_update(app: App) -> datetime | None:
    times = [h.last_success for h in app.state().health.values() if h.last_success]
    return max(times, default=None)


def _banners(app: App, now: datetime) -> str:
    """Warnings for things that need my attention: stale data, failing sites, failing email."""
    messages = []
    health = app.state().health
    last = _last_update(app)
    if last and now - last > timedelta(hours=STALE_HOURS):
        hours = int((now - last).total_seconds() // 3600)
        messages.append(
            f"No update in {hours} hours. Is the PC on and the WSL crawl timer running?"
        )
    names = app.cfg.health_names()
    failing = [
        f"{names.get(cid, cid)} ({h.consecutive_failures} failed checks)"
        for cid, h in health.items()
        if h.consecutive_failures >= FAILURES_BEFORE_ALERT
    ]
    if failing:
        messages.append(f"Site checks failing: {', '.join(failing)}")
    outbox = notify.load_outbox(app.state_dir / "email.json")
    if outbox["last_error"] and outbox["pending"]:
        messages.append(
            f"Alert email failing ({len(outbox['pending'])} unsent): {outbox['last_error']}"
        )
    return "".join(f'<div class="banner">{escape(m)}</div>' for m in messages)


def next_run(now: datetime) -> datetime:
    """Next even-hour Eastern check, accounting for daylight-saving transitions."""
    now = now.astimezone(UTC)
    candidate = now.replace(minute=SCHEDULE_MINUTE, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(hours=1)
    while candidate.astimezone(EASTERN).hour % SCHEDULE_EVERY_HOURS:
        candidate += timedelta(hours=1)
    return candidate.astimezone(EASTERN)


def _updated(app: App) -> str:
    upcoming = next_run(datetime.now(UTC))
    next_text = f"next {upcoming:%-I:%M %p %Z}"
    last = _last_update(app)
    if last is None:
        return f"never updated · {next_text}"
    return f"updated {last.astimezone(EASTERN):%b %-d, %-I:%M %p %Z} · {next_text}"


def score_tier(score: int | None) -> str:
    """Color band for a 1-10 score: high 7-10, mid 4-6, low 1-3; "" when unscored."""
    if not score:
        return ""
    return "high" if score >= 7 else "mid" if score >= 4 else "low"


def new_tier(p: Posting, latest_check: datetime | None, now: datetime) -> str:
    """Badge color for a newly found posting: "latest", "recent", "older", or "" for none.

    A run stamps first_seen and last_success with the same time, so postings the latest
    check found have first_seen equal to it.
    """
    if p.status != "active" or p.first_seen <= BADGES_SINCE:
        return ""
    if latest_check and p.first_seen >= latest_check:
        return "latest"
    age = now - p.first_seen
    if age <= timedelta(hours=NEW_HOURS):
        return "recent"
    if age <= timedelta(hours=RECENT_HOURS):
        return "older"
    return ""


def added_label(first_seen: datetime, now: datetime) -> str:
    age = max(0, (now - first_seen).total_seconds())
    if age > 24 * 3600:
        return f"added {first_seen.astimezone(EASTERN):%b %-d}"
    if age < 60:
        return "added just now"
    amount = int(age // (3600 if age >= 3600 else 60))
    unit = "hour" if age >= 3600 else "minute"
    return f"added {amount} {unit}{'' if amount == 1 else 's'} ago"


def _card(r: Row, now: datetime, latest_check: datetime | None) -> str:
    p = r.posting
    c = p.classification or {}
    badges = []
    if r.group:
        badges.append(f'<span class="badge g-{r.group}">{VIEWS[r.group]}</span>')
    fresh = new_tier(p, latest_check, now)
    if fresh:
        badges.append(f'<span class="badge new {fresh}">New</span>')
    if p.status == "removed":
        badges.append('<span class="badge closed">Closed</span>')
    if p.area == "extended":
        badges.append('<span class="badge outside">Outside Upstate</span>')
    score = c.get("score")
    tier = score_tier(score)
    score_html = (
        f'<span class="score" title="Similarity to target role">{score}/10</span>' if score else ""
    )
    when = f"posted {p.posted_date:%b %-d}" if p.posted_date else "posting date unknown"
    level = c.get("level", "unknown")
    far = f"{r.miles:.0f} mi" if r.miles is not None else ""
    meta_parts = [escape(x) for x in (p.location, far, level if level != "unknown" else "") if x]
    meta_parts.append(f'<span class="posted">{escape(when)}</span>')
    meta = " · ".join(meta_parts)
    reason = escape(c.get("reason", ""))
    note = escape(r.marks.get("note", ""))
    key = escape(p.key)
    href = escape(notify.safe_url(p.url))

    def toggle(field: str, on: str, off: str) -> str:
        active = r.marks.get(field, False)
        return (
            f'<button type="button" class="toggle{" on" if active else ""}" data-field="{field}" '
            f'aria-pressed="{str(active).lower()}" data-on="{on}" data-off="{off}">'
            f"{on if active else off}</button>"
        )

    return f"""
<article class="card{f" tier-{tier}" if tier else ""}" data-key="{key}">
  <label class="card-selector">
    <input class="card-check" type="checkbox" aria-label="Select {escape(p.title, quote=True)}">
  </label>
  <div class="company-row">
    <p class="company">{escape(r.source)}</p>
    <time class="added" datetime="{p.first_seen.isoformat()}"
      data-date="added {p.first_seen.astimezone(EASTERN):%b %-d}"
      title="Added {p.first_seen.astimezone(EASTERN):%b %-d, %Y, %-I:%M %p %Z}">
      {added_label(p.first_seen, now)}</time>
  </div>
  <div class="title-row">
    <h2><a href="{href}" target="_blank" rel="noopener">{escape(p.title)}</a></h2>
    {score_html}
  </div>
  <p class="meta">{meta}</p>
  {
        salaries.card(p)
        if r.group in ("exact", "near", "maybe", "evaluate")
        and not r.marks.get("hidden")
        and r.employer_status != "blocked"
        else ""
    }
  {f'<div class="badges">{"".join(badges)}</div>' if badges else ""}
  {_also_on(r)}
  {f'<p class="reason">{reason}</p>' if reason else ""}
  {_employer_panel(r)}
  <div class="actions">
    {toggle("starred", "★ Starred", "☆ Star")}
    {toggle("applied", "✓ Applied", "Applied")}
    {toggle("hidden", "Unhide", "Hide")}
  </div>
  <details{" open" if note else ""}>
    <summary>Notes</summary>
    <textarea rows="3" maxlength="{userdata.MAX_NOTE}" placeholder="Notes">{note}</textarea>
    <span class="saved" aria-live="polite"></span>
  </details>
</article>"""


def _also_on(r: Row) -> str:
    if not r.also_on:
        return ""
    links = ", ".join(
        f'<a href="{escape(notify.safe_url(url))}" target="_blank" rel="noopener">'
        f"{escape(name)}</a>"
        for name, url in r.also_on
    )
    return f'<p class="also-on">Also listed on {links}</p>'


def _verify_links(r: Row) -> str:
    name = r.posting.employer or r.posting.title
    links = {
        "Google": f"https://www.google.com/search?q={quote_plus(f'"{name}" {r.posting.location}')}",
        "LinkedIn": f"https://www.linkedin.com/search/results/companies/?keywords={quote_plus(name)}",
        "Glassdoor": f"https://www.glassdoor.com/Search/results.htm?keyword={quote_plus(name)}",
        "SC business search": "https://businessfilings.sc.gov/BusinessFiling/Entity/Search",
    }
    return " · ".join(
        f'<a href="{escape(url)}" target="_blank" rel="noopener">{label}</a>'
        for label, url in links.items()
    )


def _employer_panel(r: Row) -> str:
    """Approve/block controls for job-board employers that need my call."""
    if r.employer_status not in ("unverified", "blocked"):
        return ""
    note = escape((r.posting.classification or {}).get("employer_note", ""))
    key = escape(r.employer_key)

    def button(action: str, label: str, primary: bool = False) -> str:
        cls = "btn primary employer-action" if primary else "btn employer-action"
        return (
            f'<button type="button" class="{cls}" data-employer="{key}" '
            f'data-action="{action}">{label}</button>'
        )

    if r.employer_status == "unverified":
        text = f"Unverified employer{f': {note}' if note else ''}"
        buttons = button("approve", "Approve", primary=True) + button("block", "Block")
    else:
        text = "Blocked employer"
        buttons = button("clear", "Unblock") + button("approve", "Approve")
    return f"""<div class="employer-panel">
    <p>{text}</p>
    <p class="verify-links">Check: {_verify_links(r)}</p>
    <div class="actions">{buttons}</div>
  </div>"""


def render_page(
    app: App,
    view: str,
    source: str,
    q: str,
    sort: str,
    counts: dict[str, int],
    shown: list[Row],
    company: str = "",
) -> str:
    now = datetime.now(UTC)
    latest_check = _last_update(app)
    views = VIEWS
    tabs = "".join(
        f'<a class="tab{" active" if v == view else ""}" data-view="{v}" '
        f"{'aria-current=page ' if v == view else ''}"
        f'data-move-target="{v if v in MOVE_DESTINATIONS else ""}" '
        f'href="{escape(_link(v, source, q, sort))}">'
        f"{label} <span>{counts[v]}</span></a>"
        for v, label in views.items()
    )
    source_opts = "".join(
        f'<option value="{s}"{" selected" if s == source else ""}>{label}</option>'
        for s, label in SOURCES.items()
    )
    sort_opts = "".join(
        f'<option value="{k}"{" selected" if k == sort else ""}>{label}</option>'
        for k, label in SORTS.items()
    )
    cards = "".join(_card(r, now, latest_check) for r in shown)
    if view == "industrial":
        cards = (
            '<p class="review-intro">Industrial programming, automation and controls roles '
            "for your review. Use Move to mark a job Exact or Close, or Hide it.</p>"
        ) + cards
    if view == "companies":
        if company in app.companies:
            name = app.cfg.health_names()[company]
            cards = (
                f'<div class="company-heading"><a href="{escape(_link(view, source, q, sort))}">'
                f"← All companies</a><h2>{escape(name)}</h2><p>{len(shown)} current postings "
                "matching your filters</p></div>"
                f'<div class="compact-jobs">{cards or "<p>No current postings match.</p>"}</div>'
            )
        else:
            cards = company_directory(app, source, q, sort)
    cards = cards or '<p class="empty">Nothing here.</p>'
    return PAGE.format(
        head_script=HEAD_SCRIPT,
        onboarding_form=onboarding.FORM.format(
            locations="".join(
                f'<option value="{escape(region)}"'
                + (" selected" if region == "upstate_sc" else "")
                + f">{escape(region.replace('_', ' ').title())}</option>"
                for region in app.cfg.regions
            )
            + '<option value="">All locations</option>'
        ),
        onboarding_script=onboarding.SCRIPT,
        font_head=_font_select("head", "Company &amp; position"),
        font_body=_font_select("body", "Rest of page"),
        updated=escape(_updated(app)),
        title="Job Scraper",
        page_link='<a class="btn" href="/jev">Jev page</a>',
        banner=_banners(app, now),
        tabs=tabs,
        view=escape(view),
        view_label=escape(views[view]),
        source_opts=source_opts,
        sort_opts=sort_opts,
        q=escape(q),
        cards=cards,
        move_toolbar=(
            '<div class="move-toolbar"><button type="button" class="btn" id="move-mode" '
            'aria-pressed="false">Move</button><div id="move-controls" hidden>'
            '<span id="move-count" aria-live="polite">0 selected</span>'
            '<button type="button" class="btn" id="select-all">Select all</button></div>'
            '<p id="move-status" role="status" hidden></p></div>'
            if shown
            else ""
        ),
        company_input=(
            f'<input type="hidden" name="company" value="{escape(company, quote=True)}">'
            if view == "companies" and company in app.companies
            else ""
        ),
        reverse_link=escape(
            _link(
                view,
                source,
                q,
                REVERSE_SORT[sort],
                company if view == "companies" else "",
            )
        ),
        reverse_label=escape(SORTS[REVERSE_SORT[sort]]),
        footer=escape(usage.summary(jsonfile.read(app.state_dir / "usage.json"), now)),
    )


def company_directory(app: App, source: str, q: str, sort: str) -> str:
    rows = app.rows()
    names = app.cfg.health_names()
    cards = []
    for company in sorted(
        app.directory_companies(source, q, rows), key=lambda c: names[c.id].casefold()
    ):
        owned = [r for r in rows if r.posting.company_id == company.id]
        current = [r for r in owned if app.current_company_job(r)]
        if (
            q
            and not matches_text(names[company.id], q)
            and not any(app.matches_query(r, q) for r in current)
        ):
            continue
        kind, label, detail = tracked.status(
            company, app.state().health.get(company.id), datetime.now(UTC), STALE_HOURS
        )
        symbols = {"good": "✓", "warning": "⚠", "bad": "✕", "muted": "○"}
        applied = sum(bool(r.marks.get("applied")) for r in owned)
        starred = sum(bool(r.marks.get("starred")) for r in owned)
        href = escape(_link("companies", source, q, sort, company.id))
        cards.append(
            f'<a class="company-card" href="{href}"><h2>{escape(names[company.id])}</h2>'
            f'<div class="company-status {kind}" title="{escape(detail, quote=True)}">'
            f'{symbols[kind]} {escape(label)}</div><div class="company-counts">'
            f"<div><strong>{len(current)}</strong><span>Matching current jobs</span></div>"
            f"<div><strong>{applied}</strong><span>Applied · all time</span></div>"
            f"<div><strong>{starred}</strong><span>Starred · all time</span></div>"
            "</div></a>"
        )
    return '<div class="company-grid">' + "".join(cards) + "</div>"


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
{head_script}
<style>
:root {{
  color-scheme: light dark;
  --bg: #f4f5f7; --card: #fff; --text: #16191d; --muted: #5b6470; --line: #e1e4e8;
  --accent: #2557d6; --accent-text: #fff; --warn: #b42318;
  --high: #1a7f37; --mid: #9a6700; --low: #c62828; --unscored: #16191d;
  --exact: var(--high); --near: var(--mid); --maybe: #6e7781;
  --shadow: 0 1px 2px rgb(16 24 40 / .06);
  --radius: 10px;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #0e1115; --card: #161a20; --text: #e6e9ed; --muted: #98a2b0; --line: #262c34;
    --accent: #7aa2ff; --accent-text: #0e1115; --warn: #ff7b72;
    --high: #4ac26b; --mid: #e3b341; --low: #ff7b72; --unscored: #e6e9ed;
    --maybe: #98a2b0;
    --shadow: none;
  }}
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--text); font-size: 16px; line-height: 1.5;
  font-family: var(--font-body, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif);
  -webkit-text-size-adjust: 100%; }}
button, input, select, textarea {{ font: inherit; color: inherit; }}
a {{ color: var(--accent); }}
:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}

/* Header */
header {{ position: relative; z-index: 10; background: var(--bg);
  border-bottom: 1px solid var(--line); padding: 0 16px;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }}
.header-inner {{ max-width: 960px; margin: 0 auto; }}
.top {{ display: flex; justify-content: space-between; align-items: center;
  gap: 16px; min-height: 72px; }}
.brand {{ min-width: 0; }}
.top-controls {{ display: flex; align-items: center; gap: 8px; }}
h1 {{ font-size: 1.1rem; font-weight: 700; letter-spacing: -.03em; margin: 0;
  white-space: nowrap; }}
.updated {{ display: block; color: var(--muted); font-size: .72rem; margin-top: 3px; }}
.current-view {{ display: none; color: var(--muted); font-size: .75rem; }}
.header-actions {{ display: flex; align-items: center; gap: 8px; padding: 4px 0 12px; }}
.header-actions .btn {{ display: inline-flex; align-items: center; justify-content: center;
  text-decoration: none; white-space: nowrap; font-size: .78rem;
  min-height: 36px; padding: 0 10px; }}
.header-actions a {{ border-color: transparent; color: var(--muted); }}
.header-actions a:hover {{ color: var(--text); background: var(--card); }}
.cog {{ flex: none; width: 36px; height: 36px; font-size: 1.1rem; cursor: pointer;
  border: 1px solid var(--line); border-radius: 8px; background: var(--card); }}
.menu-toggle {{ display: none; align-items: center; gap: 8px; min-height: 40px;
  border: 1px solid var(--line); background: var(--card); border-radius: 8px;
  padding: 0 12px; font-size: .8rem; cursor: pointer; }}
.menu-toggle svg {{ width: 16px; height: 16px; }}
form.filters {{ display: grid; grid-template-columns: minmax(0,.8fr) minmax(0,1.2fr);
  gap: 8px; margin: 0 0 12px; }}
form.filters input, form.filters select {{ width: 100%; min-width: 0; height: 42px; padding: 0 12px;
  border: 1px solid var(--line); border-radius: 8px; background: var(--card); color: var(--text);
  font-size: 16px; }}
form.filters input {{ grid-column: 1 / -1; }}
nav.tabs {{ display: flex; gap: 4px; overflow-x: auto; scrollbar-width: none;
  padding: 0 0 10px; -webkit-overflow-scrolling: touch; }}
nav.tabs::-webkit-scrollbar {{ display: none; }}
nav.tabs.can-drag {{ cursor: grab; user-select: none; }}
nav.tabs.can-drag .tab {{ cursor: pointer; -webkit-user-drag: none; }}
nav.tabs.dragging, nav.tabs.dragging .tab {{ cursor: grabbing; }}
:root {{
  --tab-green: #1c5b39; --tab-amber: #6e4e14; --tab-blue: #42577a;
  --tab-gold: #6d512a; --tab-teal: #20585d; --tab-rose: #83424e;
  --tab-neutral: #525a66; --tab-slate: #4a566c;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --tab-green: #91baa0; --tab-amber: #c6ad7c; --tab-blue: #9aaed0;
    --tab-gold: #cab77d; --tab-teal: #80b7b1; --tab-rose: #bf8f9a;
    --tab-neutral: #98a2b0; --tab-slate: #a2aec5;
  }}
}}
.tab {{ --tab-tone: var(--tab-neutral); white-space: nowrap; padding: 8px 10px;
  color: var(--tab-tone); text-decoration: none; border: 1px solid transparent;
  background: color-mix(in srgb, var(--tab-tone) 5%, transparent);
  border-radius: 7px; font-size: .78rem; font-weight: 500; }}
.tab[data-view="exact"] {{ --tab-tone: var(--tab-green); }}
.tab[data-view="near"], .tab[data-view="unverified"] {{ --tab-tone: var(--tab-amber); }}
.tab[data-view="relevant"] {{ --tab-tone: var(--tab-blue); }}
.tab[data-view="industrial"] {{ --tab-tone: #ab98c8; }}
.tab[data-view="starred"] {{ --tab-tone: var(--tab-gold); }}
.tab[data-view="applied"] {{ --tab-tone: var(--tab-teal); }}
.tab[data-view="blocked"] {{ --tab-tone: var(--tab-rose); }}
.tab[data-view="companies"] {{ --tab-tone: var(--tab-slate); }}
.tab:hover {{ background: color-mix(in srgb, var(--tab-tone) 11%, transparent);
  border-color: color-mix(in srgb, var(--tab-tone) 26%, transparent); }}
.tab span {{ display: inline-block; min-width: 1.5em; margin-left: 4px; padding: 0 5px;
  border-radius: 5px; background: color-mix(in srgb, var(--tab-tone) 6%, transparent);
  font-size: .7rem; text-align: center; font-variant-numeric: tabular-nums; }}
.tab.active {{ background: color-mix(in srgb, var(--tab-tone) 16%, transparent);
  border-color: color-mix(in srgb, var(--tab-tone) 45%, transparent);
  box-shadow: inset 0 -2px 0 var(--tab-tone); font-weight: 650; }}
.header-space {{ display: none; }}
@media (max-width: 639px) {{
  .js-header header {{ position: sticky;
    top: min(0px, calc(100dvh - var(--header-visible-height)));
    box-shadow: var(--shadow); }}
  .js-header .header-space {{ display: block;
    height: max(0px, calc(var(--header-height) - var(--header-visible-height))); }}
  .js-header .menu-toggle {{ display: inline-flex; }}
  .header-actions {{ flex-wrap: wrap; gap: 4px; }}
  header.compact .header-content {{ display: none; }}
  header.compact .top {{ min-height: 60px; }}
  header.compact .updated {{ display: none; }}
  header.compact .current-view {{ display: block; }}
}}
.banner {{ background: var(--warn); color: #fff; padding: 8px 12px; border-radius: 8px;
  margin-top: 10px; font-size: .88rem; }}

.sort-control {{ display: flex; gap: 6px; min-width: 0; }}
.reverse-sort {{ display: grid; place-items: center; flex: none; width: 42px;
  border: 1px solid var(--line); border-radius: 8px; color: var(--text);
  background: var(--card); text-decoration: none; font-size: 1.2rem; }}
.search-help {{ font-size: .7rem; color: var(--muted); margin: -4px 0 10px; }}
/* Bulk move */
.move-toolbar {{ display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  margin-bottom: 14px; font-size: .82rem; }}
#move-controls {{ display: flex; align-items: center; gap: 12px; }}
#move-status {{ flex-basis: 100%; margin: 0; color: var(--muted); font-size: .78rem; }}
#move-status.error {{ color: var(--warn); }}
.move-toolbar [hidden] {{ display: none !important; }}
.card {{ position: relative; }}
.card-selector {{ display: none; }}
body.move-mode .card {{ padding-left: 52px; }}
body.move-mode .card-selector {{ display: block; position: absolute;
  left: 8px; top: 10px; padding: 8px; }}
.card-check {{ appearance: none; -webkit-appearance: none; display: grid; place-content: center;
  width: 22px; height: 22px; margin: 0; border: 1.5px solid var(--muted);
  border-radius: 6px; background: var(--bg); cursor: pointer; }}
.card-check:checked {{ background: var(--accent); border-color: var(--accent); }}
.card-check::before {{ content: ""; width: 5px; height: 10px; border: solid var(--accent-text);
  border-width: 0 2px 2px 0; transform: translateY(-1px) rotate(45deg); opacity: 0; }}
.card-check:checked::before {{ opacity: 1; }}
.card.selected {{ outline: 1px solid var(--accent); outline-offset: 1px; }}
body.move-mode .tab[aria-disabled="true"] {{ opacity: .45; cursor: not-allowed; }}
body.move-mode .tab[data-move-target]:not([data-move-target=""]):hover {{
  outline: 1px solid var(--tab-tone); outline-offset: -1px; }}
.company-grid {{ display: grid;
  grid-template-columns: repeat(auto-fit,minmax(260px,1fr)); gap: 12px; }}
.company-card {{ display: block; color: var(--text); background: var(--card); padding: 18px;
  border: 1px solid var(--line); border-radius: 10px; text-decoration: none; }}
.company-card:hover {{ border-color: var(--accent); }}
.company-card h2 {{ font-size: 1rem; margin: 0 0 8px; }}
.company-status {{ color: var(--muted); font-size: .75rem; }}
.company-status.good {{ color: var(--high); }}
.company-status.warning {{ color: var(--mid); }}
.company-status.bad {{ color: var(--low); }}
.company-counts {{ display: grid; grid-template-columns: 1fr 1fr;
  gap: 12px; margin-top: 14px; }}
.company-counts > div:first-child {{ grid-column: 1 / -1; }}
.company-counts strong {{ display: block; font-size: 1.1rem; }}
.company-counts span {{ font-size: .72rem; color: var(--muted); }}
.company-heading {{ margin-bottom: 16px; }}
.company-heading h2 {{ margin: 8px 0 4px; }}
.company-heading p {{ font-size: .8rem; color: var(--muted); }}
.compact-jobs .card {{ padding: 12px 14px; margin-bottom: 8px; }}
.compact-jobs .card h2 {{ font-size: .95rem; }}
.compact-jobs .company,.compact-jobs .meta,.compact-jobs .reason {{ font-size: .76rem; }}
.compact-jobs .salary {{ font-size: .74rem; }}
.compact-jobs .actions {{ margin-top: 8px; }}
.compact-jobs .toggle {{ min-height: 34px; font-size: .75rem; }}
@media(max-width:360px) {{ .company-grid {{ grid-template-columns: 1fr; }} }}

/* Cards */
main {{ max-width: 992px; margin: 0 auto; padding: 12px 16px 48px; }}
.card {{ background: var(--card); border: 1px solid var(--line); border-left: 4px solid var(--line);
  border-radius: var(--radius); padding: 14px 14px 12px; margin-bottom: 10px;
  box-shadow: var(--shadow); --tier: var(--unscored); }}
.card.tier-high {{ --tier: var(--high); border-left-color: var(--high); }}
.card.tier-mid {{ --tier: var(--mid); border-left-color: var(--mid); }}
.card.tier-low {{ --tier: var(--low); border-left-color: var(--low); }}
.company, .card h2 {{ font-family: var(--font-head, inherit); }}
.company {{ margin: 0; font-size: .82rem; font-weight: 600; color: var(--muted);
  text-transform: uppercase; letter-spacing: .04em; }}
.company-row {{ display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }}
.added {{ flex: none; color: var(--muted); font-size: .78rem; white-space: nowrap; }}
.title-row {{ display: flex; align-items: flex-start; gap: 10px; margin: 2px 0 4px; }}
.card h2 {{ flex: 1; min-width: 0; margin: 0; font-size: 1.08rem; font-weight: 700;
  line-height: 1.3; overflow-wrap: anywhere; }}
.card h2 a {{ color: var(--tier); text-decoration: none; }}
.card h2 a:hover {{ text-decoration: underline; text-underline-offset: 3px; }}
.score {{ flex: none; margin-top: 1px; padding: 2px 8px; border-radius: 999px; font-size: .78rem;
  font-weight: 700; font-variant-numeric: tabular-nums; color: var(--tier);
  background: color-mix(in srgb, var(--tier) 14%, transparent); }}
.meta {{ color: var(--muted); font-size: .88rem; margin: 0; }}
.salary {{ font-size: .82rem; color: var(--muted); margin: 8px 0 0; }}
.salary span {{ display: block; font-size: .72rem; letter-spacing: .02em; }}
.salary strong {{ color: var(--text); font-weight: 600; }}
.posted {{ white-space: nowrap; }}
.badges {{ display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }}
.badge {{ font-size: .7rem; font-weight: 600; text-transform: uppercase; letter-spacing: .04em;
  padding: 1px 7px; border-radius: 999px; border: 1px solid currentColor; }}
.g-exact {{ color: var(--exact); }} .g-near {{ color: var(--near); }}
.g-maybe {{ color: var(--maybe); }}
.g-evaluate {{ color: var(--mid); }}
.g-industrial {{ color: #ab98c8; }}
.new {{ border-color: transparent; }}
.new.latest {{ background: #d1242f; color: #fff; }}
.new.recent {{ background: #f08c00; color: #1f1300; }}
.new.older {{ background: #f5c400; color: #1f1300; }}
.closed {{ color: var(--warn); }} .outside {{ color: var(--muted); }}
.reason {{ font-size: .9rem; line-height: 1.5; margin: 10px 0 0; padding-top: 10px;
  border-top: 1px solid var(--line); }}
.also-on {{ font-size: .82rem; color: var(--muted); margin: 4px 0 0; }}

/* Controls */
.actions {{ display: flex; gap: 6px; margin-top: 12px; flex-wrap: wrap; }}
.toggle, .btn {{ min-height: 38px; padding: 0 12px; border-radius: 8px; cursor: pointer;
  font-size: .86rem; font-weight: 500; border: 1px solid var(--line); background: transparent;
  color: var(--text); }}
.toggle {{ flex: 1; }}
.toggle.on, .btn.primary {{ background: var(--accent); border-color: var(--accent);
  color: var(--accent-text); }}
.toggle:disabled, .btn:disabled {{ opacity: .6; cursor: progress; }}
details {{ margin-top: 10px; font-size: .9rem; }}
summary {{ color: var(--muted); cursor: pointer; padding: 4px 0; }}
textarea {{ width: 100%; margin-top: 6px; padding: 8px 10px; border-radius: 8px;
  border: 1px solid var(--line); background: var(--bg); color: var(--text); resize: vertical; }}
.saved {{ color: var(--muted); font-size: .8rem; }}
.card.leaving {{ opacity: 0; transform: translateX(16px); transition: opacity .2s, transform .2s; }}
.empty {{ color: var(--muted); text-align: center; margin-top: 48px; }}
.employer-panel {{ margin-top: 12px; padding: 10px 12px; border-radius: 8px;
  background: color-mix(in srgb, var(--mid) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--mid) 40%, transparent); font-size: .88rem; }}
.employer-panel p {{ margin: 0 0 4px; }}
.employer-panel .actions {{ margin-top: 8px; }}
footer {{ max-width: 760px; margin: 0 auto; padding: 0 16px 32px; color: var(--muted);
  font-size: .8rem; text-align: center; }}

/* Settings */
dialog {{ border: 1px solid var(--line); border-radius: 12px; background: var(--card);
  color: var(--text); width: min(92vw, 380px); padding: 18px; }}
dialog::backdrop {{ background: rgb(0 0 0 / .45); }}
dialog h2 {{ margin: 0 0 14px; font-size: 1.05rem; }}
dialog label {{ display: block; font-size: .88rem; color: var(--muted); margin-bottom: 14px; }}
dialog input, dialog select {{ display: block; width: 100%; height: 42px;
  margin-top: 4px; padding: 0 10px;
  border: 1px solid var(--line); border-radius: 8px; background: var(--bg); color: var(--text); }}
.dialog-actions {{ display: flex; justify-content: space-between; gap: 8px; }}

@media (min-width: 900px) {{
  .header-actions {{ position: absolute; right: 48px; top: 26px; padding: 0; }}
}}

/* Wider screens */
@media (min-width: 640px) {{
  header {{ padding: 0 24px; }}
  .header-inner {{ position: relative; }}
  .top {{ min-height: 88px; }}

  h1 {{ font-size: 1.25rem; }}
  .updated {{ font-size: .72rem; }}
  form.filters {{ grid-template-columns: 1fr 170px 170px; }}
  form.filters input {{ grid-column: auto; }}
  main {{ max-width: 1008px; padding: 16px 24px 56px; }}
  .card {{ padding: 16px 20px 14px; margin-bottom: 12px; }}
  .card h2 {{ font-size: 1.15rem; }}
  .toggle {{ flex: none; }}
}}
</style>
</head>
<body>
<header>
  <div class="header-inner">
  <div class="top">
    <div class="brand">
      <h1>{title}</h1>
      <span class="updated">{updated}</span>
      <span class="current-view">{view_label}</span>
    </div>
    <div class="top-controls">
    <button type="button" class="cog" id="open-settings" aria-label="Settings">⚙</button>
    <button type="button" class="menu-toggle" id="header-toggle"
      aria-controls="header-content" aria-expanded="true">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
        aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16"/></svg>
      <span>Menu</span>
    </button>
    </div>
  </div>
  <div class="header-content" id="header-content">
    <nav class="header-actions" aria-label="Manage job search">
      <a class="btn" href="/companies">Tracked companies</a>
      <a class="btn" href="/logs">Crawl logs</a>
      {page_link}
      <button type="button" class="btn primary" id="open-company">Add company</button>
    </nav>
  {banner}
  <form class="filters" method="get">
    <input type="hidden" name="view" value="{view}">
    {company_input}
    <input type="search" name="q" value="{q}" aria-label="Search jobs"
      aria-describedby="search-help"
      placeholder="Search title, employer, place">
    <select aria-label="Source" name="source" onchange="this.form.submit()">{source_opts}</select>
    <div class="sort-control"><select aria-label="Sort jobs" name="sort"
      onchange="this.form.submit()">{sort_opts}</select>
      <a class="reverse-sort" href="{reverse_link}" aria-label="Reverse sort: {reverse_label}"
         title="{reverse_label}">↕</a></div>
  </form>
  <p class="search-help" id="search-help">Exclude with !desktop or !"help desk".
    Salary sorts use the lower estimate; hourly rates × 2,080 for comparison.</p>
  <nav class="tabs" aria-label="Job views" tabindex="0">{tabs}</nav>
  </div>
  </div>
</header>
<div class="header-space" aria-hidden="true"></div>
<main>{move_toolbar}{cards}</main>
<footer>{footer}</footer>
{onboarding_form}
<dialog id="settings">
  <h2>Settings</h2>
  {font_head}
  {font_body}
  <div class="dialog-actions">
    <button type="button" class="btn" id="reset-fonts">Reset</button>
    <button type="button" class="btn primary" id="close-settings">Done</button>
  </div>
</dialog>
<script>
{onboarding_script}
const header = document.querySelector("header");
function updateAddedTimes() {{
  const now = Date.now();
  document.querySelectorAll("time.added").forEach((label) => {{
    const age = Math.max(0, (now - Date.parse(label.dateTime)) / 1000);
    if (!Number.isFinite(age)) return;
    if (age > 86400) label.textContent = label.dataset.date;
    else if (age < 60) label.textContent = "added just now";
    else {{
      const hours = age >= 3600;
      const amount = Math.floor(age / (hours ? 3600 : 60));
      const unit = hours ? "hour" : "minute";
      label.textContent = `added ${{amount}} ${{unit}}${{amount === 1 ? "" : "s"}} ago`;
    }}
  }});
}}
updateAddedTimes();
setInterval(updateAddedTimes, 30000);
document.addEventListener("visibilitychange", () => {{
  if (!document.hidden) updateAddedTimes();
}});
const headerToggle = document.getElementById("header-toggle");
const mobileHeader = matchMedia("(max-width: 639px)");
let menuPinned = false;
function setHeaderCompact(compact) {{
  header.classList.toggle("compact", compact);
  headerToggle.setAttribute("aria-expanded", String(!compact));
  // Keep the cards in place as the in-flow header changes height.
  document.documentElement.style.setProperty("--header-visible-height", header.offsetHeight + "px");
}}
function syncHeader() {{
  if (!mobileHeader.matches) {{
    setHeaderCompact(false);
    return;
  }}
  if (window.scrollY < 40) menuPinned = false;
  const editing = header.querySelector(".header-content").contains(document.activeElement);
  setHeaderCompact(window.scrollY > 100 && !menuPinned && !editing);
}}
function measureHeader() {{
  const compact = header.classList.contains("compact");
  setHeaderCompact(false);
  document.documentElement.style.setProperty("--header-height", header.offsetHeight + "px");
  setHeaderCompact(compact);
  syncHeader();
  const selectedTab = header.querySelector(".tab.active");
  const tabBar = header.querySelector("nav.tabs");
  tabBar.classList.toggle("can-drag", tabBar.scrollWidth > tabBar.clientWidth);
  if (selectedTab) {{
    const barRect = tabBar.getBoundingClientRect();
    const tabRect = selectedTab.getBoundingClientRect();
    if (tabRect.left < barRect.left) tabBar.scrollLeft -= barRect.left - tabRect.left + 8;
    else if (tabRect.right > barRect.right) tabBar.scrollLeft += tabRect.right - barRect.right + 8;
  }}
}}
document.documentElement.classList.add("js-header");
measureHeader();
window.addEventListener("resize", measureHeader);
window.addEventListener("scroll", syncHeader, {{passive: true}});
header.addEventListener("focusout", () => setTimeout(syncHeader, 0));
headerToggle.addEventListener("click", () => {{
  const opening = header.classList.contains("compact");
  menuPinned = opening;
  setHeaderCompact(!opening);
}});
document.addEventListener("keydown", (event) => {{
  if (event.key === "Escape" && mobileHeader.matches && !document.querySelector("dialog[open]")) {{
    menuPinned = false;
    headerToggle.focus();
    setHeaderCompact(true);
  }}
}});
const tabBar = document.querySelector("nav.tabs");
let tabDrag = null;
let suppressTabClick = false;
tabBar.addEventListener("pointerdown", (event) => {{
  // Touch keeps the browser's native swipe and vertical-page scrolling.
  if (event.isPrimary) suppressTabClick = false;
  if (event.pointerType === "touch" || event.button !== 0 || !event.isPrimary
      || tabBar.scrollWidth <= tabBar.clientWidth) return;
  tabDrag = {{id: event.pointerId, x: event.clientX, scroll: tabBar.scrollLeft, moved: false}};
}});
tabBar.addEventListener("pointermove", (event) => {{
  if (!tabDrag || event.pointerId !== tabDrag.id) return;
  const delta = event.clientX - tabDrag.x;
  if (!tabDrag.moved && Math.abs(delta) < 6) return;
  if (!tabDrag.moved) {{
    tabDrag.moved = true;
    tabBar.setPointerCapture(event.pointerId);
    tabBar.classList.add("dragging");
  }}
  event.preventDefault();
  tabBar.scrollLeft = tabDrag.scroll - delta;
}});
function finishTabDrag(event) {{
  if (!tabDrag || event.pointerId !== tabDrag.id) return;
  suppressTabClick = tabDrag.moved;
  tabDrag = null;
  tabBar.classList.remove("dragging");
  if (tabBar.hasPointerCapture(event.pointerId)) tabBar.releasePointerCapture(event.pointerId);
}}
tabBar.addEventListener("pointerup", finishTabDrag);
tabBar.addEventListener("pointercancel", finishTabDrag);
tabBar.addEventListener("lostpointercapture", finishTabDrag);
tabBar.addEventListener("dragstart", (event) => event.preventDefault());
tabBar.addEventListener("click", (event) => {{
  if (!suppressTabClick || event.detail === 0) return;
  event.preventDefault();
  event.stopPropagation();
  suppressTabClick = false;
}}, true);
tabBar.addEventListener("keydown", (event) => {{
  if (event.target !== tabBar || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
  event.preventDefault();
  tabBar.scrollLeft += event.key === "ArrowRight" ? 150 : -150;
}});
async function post(path, body) {{
  const res = await fetch(path + location.search, {{
    method: "POST",
    headers: {{"Content-Type": "application/json", "X-Requested-With": "jobscraper"}},
    body: JSON.stringify(body),
  }});
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}}
document.addEventListener("click", async (e) => {{
  const btn = e.target.closest(".card .toggle");
  if (!btn) return;
  const card = btn.closest(".card");
  const value = btn.getAttribute("aria-pressed") !== "true";
  btn.disabled = true;
  try {{
    const result = await post("/api/mark", {{
      key: card.dataset.key, field: btn.dataset.field, value, page: location.pathname,
    }});
    btn.setAttribute("aria-pressed", String(value));
    btn.classList.toggle("on", value);
    btn.textContent = value ? btn.dataset.on : btn.dataset.off;
    if (!result.visible) dropCard(card);
    for (const [view, n] of Object.entries(result.counts)) {{
      document.querySelector(`.tab[data-view="${{view}}"] span`).textContent = n;
    }}
  }} catch (err) {{
    alert("Couldn't save: " + err.message);
  }} finally {{
    btn.disabled = false;
  }}
}});
const moveButton = document.getElementById("move-mode");
let moveMode = false;
let moveBusy = false;
const selectedCards = () => [...document.querySelectorAll(".card-check:checked")];
function updateMoveSelection() {{
  const checked = selectedCards();
  document.getElementById("move-count").textContent = `${{checked.length}} selected`;
  document.querySelectorAll(".card").forEach(card =>
    card.classList.toggle("selected", card.querySelector(".card-check").checked));
  document.querySelectorAll(".tab").forEach(tab => {{
    if (moveMode && checked.length && !tab.dataset.moveTarget)
      tab.setAttribute("aria-disabled", "true");
    else tab.removeAttribute("aria-disabled");
  }});
  document.getElementById("select-all").textContent =
    checked.length === document.querySelectorAll(".card-check").length
      ? "Clear selection" : "Select all";
}}
if (moveButton) {{
  moveButton.addEventListener("click", () => {{
    if (moveBusy) return;
    moveMode = !moveMode;
    document.body.classList.toggle("move-mode", moveMode);
    moveButton.setAttribute("aria-pressed", String(moveMode));
    moveButton.textContent = moveMode ? "Cancel move" : "Move";
    document.getElementById("move-controls").hidden = !moveMode;
    const status = document.getElementById("move-status");
    status.hidden = !moveMode;
    status.classList.remove("error");
    status.textContent = "Select cards, then click Exact, Close, Probably not, " +
      "Industrial / controls, Starred, Applied, or Hidden.";
    document.querySelectorAll(".card-check").forEach(box => box.checked = false);
    updateMoveSelection();
  }});
  document.getElementById("select-all").addEventListener("click", () => {{
    if (moveBusy) return;
    const select = selectedCards().length !== document.querySelectorAll(".card-check").length;
    document.querySelectorAll(".card-check").forEach(box => box.checked = select);
    updateMoveSelection();
  }});
  document.addEventListener("change", event => {{
    if (event.target.matches(".card-check")) updateMoveSelection();
  }});
  tabBar.addEventListener("click", async event => {{
    const tab = event.target.closest(".tab");
    if (!moveMode || !tab || !selectedCards().length || event.defaultPrevented) return;
    event.preventDefault();
    if (moveBusy) return;
    const status = document.getElementById("move-status");
    status.classList.remove("error");
    if (!tab.dataset.moveTarget) {{
      status.textContent = "Choose a rating tab, Starred, Applied, or Hidden as the destination.";
      return;
    }}
    moveBusy = true;
    moveButton.disabled = true;
    document.querySelectorAll(".card-check, #select-all, .card .toggle")
      .forEach(el => el.disabled = true);
    status.textContent = "Moving selected postings…";
    try {{
      await post("/api/move", {{keys: selectedCards().map(box => box.closest(".card").dataset.key),
                                destination: tab.dataset.moveTarget}});
      location.assign(tab.href);
    }} catch (err) {{
      status.classList.add("error");
      status.textContent = "Couldn't move postings: " + err.message;
      moveBusy = false;
      moveButton.disabled = false;
      document.querySelectorAll(".card-check, #select-all, .card .toggle")
        .forEach(el => el.disabled = false);
    }}
  }});
}}
function dropCard(card) {{
  const main = card.closest("main");
  card.classList.add("leaving");
  setTimeout(() => {{
    card.remove();
    if (moveButton && moveMode) updateMoveSelection();
    if (!main.querySelector(".card")) main.innerHTML = '<p class="empty">Nothing here.</p>';
  }}, 200);
}}
const settings = document.getElementById("settings");
const fontSelects = settings.querySelectorAll("select[data-slot]");
function syncFontSelects() {{
  const saved = jobFonts.load();
  fontSelects.forEach((s) => (s.value = saved[s.dataset.slot]?.name || ""));
}}
document.getElementById("open-settings").addEventListener("click", () => {{
  syncFontSelects();
  settings.showModal();
}});
document.getElementById("close-settings").addEventListener("click", () => settings.close());
document.getElementById("reset-fonts").addEventListener("click", () => {{
  jobFonts.save({{}});
  syncFontSelects();
}});
fontSelects.forEach((s) =>
  s.addEventListener("change", () => {{
    const saved = jobFonts.load();
    const opt = s.selectedOptions[0];
    if (s.value) saved[s.dataset.slot] = {{name: s.value, cat: opt.dataset.cat}};
    else delete saved[s.dataset.slot];
    jobFonts.save(saved);
  }})
);
document.addEventListener("click", async (e) => {{
  const btn = e.target.closest(".employer-action");
  if (!btn) return;
  btn.disabled = true;
  try {{
    await post("/api/employer", {{employer: btn.dataset.employer, action: btn.dataset.action}});
    location.reload();  // every posting from this employer moves tabs
  }} catch (err) {{
    alert("Couldn't save: " + err.message);
    btn.disabled = false;
  }}
}});
document.addEventListener("change", async (e) => {{
  if (e.target.tagName !== "TEXTAREA") return;
  const card = e.target.closest(".card");
  if (!card) return;
  const status = card.querySelector(".saved");
  try {{
    await post("/api/mark", {{key: card.dataset.key, field: "note", value: e.target.value}});
    status.textContent = "Saved";
  }} catch (err) {{
    status.textContent = "Couldn't save: " + err.message;
  }}
}});
</script>
</body>
</html>
"""


# --- HTTP ---


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: str, content_type: str) -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            url = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path == "/":
                self._send(HTTPStatus.OK, app.index(query), "text/html")
            elif url.path == "/jev":
                self._send(HTTPStatus.OK, app.index(query, "jev"), "text/html")
            elif url.path == "/companies":
                app.reload_config()
                if query.get("company") and query["company"] not in app.companies:
                    self._send(HTTPStatus.NOT_FOUND, "Company not found", "text/plain")
                    return
                page = (
                    tracked.jobs_page(app.cfg, app.state(), query, stale_hours=STALE_HOURS)
                    if query.get("company")
                    else tracked.page(app.cfg, app.state(), stale_hours=STALE_HOURS)
                )
                self._send(HTTPStatus.OK, page, "text/html")
            elif url.path == "/logs":
                self._send(
                    HTTPStatus.OK,
                    activity.page(
                        [
                            app.state_dir / "crawl-history.json",
                            app.state_dir / "description-history.json",
                            app.marks_path.parent / "discovery-history.json",
                        ],
                        query.get("id", ""),
                    ),
                    "text/html",
                )
            else:
                self._send(HTTPStatus.NOT_FOUND, "Not found", "text/plain")

        def do_POST(self) -> None:
            url = urlparse(self.path)
            path = url.path
            if path not in (
                "/api/mark",
                "/api/move",
                "/api/employer",
                "/api/companies",
                "/api/company-name",
            ):
                self._send(HTTPStatus.NOT_FOUND, "Not found", "text/plain")
                return
            # A custom header can't be sent cross-site without a CORS preflight, which this
            # server never approves, so other sites can't change my marks.
            if self.headers.get("X-Requested-With") != "jobscraper":
                self._send(HTTPStatus.FORBIDDEN, "Forbidden", "text/plain")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 64_000:
                    raise ValueError("Invalid request size")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("Expected a JSON object")
                if path == "/api/company-name":
                    entry = app.rename_company(body)
                elif path == "/api/companies":
                    entry = app.add_company(body)
                elif path == "/api/move":
                    entry = {"moved": app.move(body["keys"], body["destination"])}
                elif path == "/api/employer":
                    app.decide_employer(body["employer"], body["action"])
                    entry = {"ok": True}
                else:
                    entry = app.mark(body["key"], body["field"], body["value"])
                    query = {k: v[0] for k, v in parse_qs(url.query).items()}
                    # V6 reloads its own results after marking; only the main page
                    # needs an inline count/visibility update.
                    if body.get("page") != "/jev":
                        shown, counts = app.select(query)
                        entry = entry | {
                            "counts": counts,
                            "visible": any(r.posting.key == body["key"] for r in shown),
                        }
            except KeyError:
                self._send(HTTPStatus.NOT_FOUND, "Unknown posting or employer", "text/plain")
                return
            except (ValueError, TypeError, httpx.HTTPError) as e:
                self._send(
                    HTTPStatus.BAD_REQUEST,
                    activity.clean(str(e)) + "\n" + "\n".join(getattr(e, "__notes__", [])),
                    "text/plain",
                )
                return
            except Exception:
                log.exception("Request failed")
                self._send(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "Unexpected error. Open Crawl logs for the attempt details.",
                    "text/plain",
                )
                return
            self._send(HTTPStatus.OK, json.dumps(entry), "application/json")

        def log_message(self, fmt: str, *args) -> None:
            log.info("%s %s", self.address_string(), fmt % args)

    return ThreadingHTTPServer((host, port), Handler)


# Runs before first paint so saved fonts apply without a flash. Font choices are a per-device
# convenience, so they live in localStorage.
HEAD_SCRIPT = """<script>
window.jobFonts = (() => {
  const KEY = "jobscraper-fonts";
  const FALLBACK = {
    sans: "system-ui, sans-serif",
    serif: "Georgia, serif",
    mono: "ui-monospace, monospace",
  };
  const VARS = {head: "--font-head", body: "--font-body"};
  function load() {
    try { return JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) { return {}; }
  }
  function ensureLoaded(name) {
    const id = "font-" + name.replace(/\\W+/g, "-");
    if (document.getElementById(id)) return;
    const link = document.createElement("link");
    link.id = id;
    link.rel = "stylesheet";
    link.href = "https://fonts.googleapis.com/css2?family=" +
      encodeURIComponent(name).replace(/%20/g, "+") + ":wght@400;700&display=swap";
    document.head.appendChild(link);
  }
  function apply(saved) {
    const root = document.documentElement.style;
    for (const [slot, cssVar] of Object.entries(VARS)) {
      const font = saved[slot];
      if (font && font.name) {
        ensureLoaded(font.name);
        root.setProperty(cssVar, `"${font.name}", ${FALLBACK[font.cat] || FALLBACK.sans}`);
      } else {
        root.removeProperty(cssVar);
      }
    }
  }
  function save(saved) {
    try { localStorage.setItem(KEY, JSON.stringify(saved)); } catch (e) {}
    apply(saved);
  }
  apply(load());
  return {load, save};
})();
</script>"""
