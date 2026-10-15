import json
import logging
import threading
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlencode, urlparse

import httpx

from jobscraper import (
    activity,
    classify,
    company_store,
    crawl,
    discovery,
    distance,
    employers,
    http,
    jsonfile,
    locations,
    notify,
    onboarding,
    state,
    tracked,
    usage,
    userdata,
)
from jobscraper import config as config_file
from jobscraper.config import Config
from jobscraper.state import Posting, State
from jobscraper.tracker import FAILURES_BEFORE_ALERT

log = logging.getLogger(__name__)

# "New" badge colors by when a posting was first seen: red if the latest check found it,
# then orange up to NEW_HOURS, yellow up to RECENT_HOURS, and no badge after that.
NEW_HOURS = 48
RECENT_HOURS = 96
# Last check before the colored badges started; postings already loaded by then get none.
BADGES_SINCE = datetime(2026, 10, 13, 17, 37, 26, 111779, tzinfo=UTC)
STALE_HOURS = 6  # no successful check for this long means the runner (or my PC) is probably off
# Matches the cron in .github/workflows/check.yml ("17 */2 * * *", UTC); a test keeps them in sync.
SCHEDULE_MINUTE = 17
SCHEDULE_EVERY_HOURS = 2

VIEWS = {
    "exact": "Exact",
    "near": "Close",
    "maybe": "Probably not",
    "relevant": "All matches",
    "unverified": "Unverified",
    "starred": "Starred",
    "applied": "Applied",
    "hidden": "Hidden",
    "blocked": "Blocked",
}
SOURCES = {"all": "All sources", "companies": "Company sites", "boards": "Job boards"}
SORTS = {"newest": "Newest", "relevant": "Most relevant", "nearest": "Nearest"}
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
        company = replace(result.company, locations=entries)
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

    def rows(self) -> list[Row]:
        st = self.state()
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
            out.append(
                Row(
                    posting=p,
                    group=classify.posting_group(p, self.cfg.profile),
                    marks=marks.get(p.key, {}),
                    source=p.source_label(company.name if company else p.company_id),
                    is_board=employers.is_board(p, self.companies),
                    employer_status=employers.status(p, self.companies, decisions),
                    employer_key=employers.employer_key(p),
                    also_on=duplicates.get(p.key, []),
                    miles=distance.miles_from(self.home, p.location),
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

    # --- filtering ---

    @staticmethod
    def in_view(r: Row, view: str) -> bool:
        active = r.posting.status == "active"
        hidden = r.marks.get("hidden", False)
        applied = r.marks.get("applied", False)
        starred = r.marks.get("starred", False)
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
        return view == "relevant" or r.group == view

    @staticmethod
    def in_source(r: Row, source: str) -> bool:
        return source == "all" or (source == "boards") == r.is_board

    @staticmethod
    def matches_query(r: Row, q: str) -> bool:
        if not q:
            return True
        hay = f"{r.posting.title} {r.source} {r.posting.location}".lower()
        return all(word in hay for word in q.lower().split())

    @staticmethod
    def sort_key(r: Row, sort: str = "newest"):
        p = r.posting
        # Newest means newest to me: job boards can pick up a posting days after it went up.
        added = -p.first_seen.timestamp()
        posted = -(p.posted_date.toordinal() if p.posted_date else 0)
        if sort == "relevant":
            # Keyword-only results have no score yet; they sort last.
            score = (p.classification or {}).get("score") or 0
            return (-score, added, posted, p.title.lower())
        if sort == "nearest":
            # Remote and unknown places sort last.
            return (r.miles is None, r.miles or 0, added, posted, p.title.lower())
        return (added, posted, p.title.lower())

    # --- pages ---

    def select(self, query: dict[str, str]) -> tuple[list[Row], dict[str, int]]:
        """Rows in the selected tab and counts for every tab, using the same filters."""
        self.reload_config()
        view = query.get("view") if query.get("view") in VIEWS else "exact"
        source = query.get("source") if query.get("source") in SOURCES else "all"
        q = query.get("q", "").strip()
        scoped = [r for r in self.rows() if self.in_source(r, source) and self.matches_query(r, q)]
        counts = {v: sum(self.in_view(r, v) for r in scoped) for v in VIEWS}
        return [r for r in scoped if self.in_view(r, view)], counts

    def index(self, query: dict[str, str]) -> str:
        view = query.get("view") if query.get("view") in VIEWS else "exact"
        source = query.get("source") if query.get("source") in SOURCES else "all"
        sort = query.get("sort") if query.get("sort") in SORTS else "newest"
        q = query.get("q", "").strip()
        shown, counts = self.select(query)
        shown.sort(key=lambda r: self.sort_key(r, sort))
        return render_page(self, view, source, q, sort, counts, shown)


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


def _link(view: str, source: str, q: str, sort: str) -> str:
    params = {"view": view, "source": source, "sort": sort}
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
        messages.append(f"No update in {hours} hours. Is the PC on and the GitHub runner running?")
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
    """Next scheduled check after `now` (UTC cron times; GitHub often starts a few minutes late)."""
    now = now.astimezone(UTC)
    candidate = now.replace(minute=SCHEDULE_MINUTE, second=0, microsecond=0)
    candidate -= timedelta(hours=candidate.hour % SCHEDULE_EVERY_HOURS)
    while candidate <= now:
        candidate += timedelta(hours=SCHEDULE_EVERY_HOURS)
    return candidate


def _updated(app: App) -> str:
    upcoming = next_run(datetime.now(UTC)).astimezone()
    next_text = f"next ~{upcoming:%-I:%M %p}"
    last = _last_update(app)
    if last is None:
        return f"never updated · {next_text}"
    return f"updated {last.astimezone():%b %-d, %-I:%M %p} · {next_text}"


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
  <div class="company-row">
    <p class="company">{escape(r.source)}</p>
    <span class="added">added {p.first_seen.astimezone():%b %-d}</span>
  </div>
  <div class="title-row">
    <h2><a href="{href}" target="_blank" rel="noopener">{escape(p.title)}</a></h2>
    {score_html}
  </div>
  <p class="meta">{meta}</p>
  {f'<div class="badges">{"".join(badges)}</div>' if badges else ""}
  {_also_on(r)}
  {f'<p class="reason">{reason}</p>' if reason else ""}
  {_employer_panel(r)}
  <div class="actions">
    {toggle("starred", "★ Starred", "☆ Star")}
    {toggle("applied", "✓ Applied", "Applied?")}
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
) -> str:
    now = datetime.now(UTC)
    latest_check = _last_update(app)
    tabs = "".join(
        f'<a class="tab{" active" if v == view else ""}" data-view="{v}" '
        f'href="{escape(_link(v, source, q, sort))}">{label} <span>{counts[v]}</span></a>'
        for v, label in VIEWS.items()
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
        banner=_banners(app, now),
        tabs=tabs,
        view=escape(view),
        source_opts=source_opts,
        sort_opts=sort_opts,
        q=escape(q),
        cards=cards,
        footer=escape(usage.summary(jsonfile.read(app.state_dir / "usage.json"), now)),
    )


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Scraper</title>
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
header {{ position: sticky; top: 0; z-index: 1; background: var(--bg);
  border-bottom: 1px solid var(--line); padding: 12px 16px 0; }}
.header-inner {{ max-width: 760px; margin: 0 auto; }}
.top {{ display: flex; flex-wrap: wrap; justify-content: space-between;
  align-items: center; gap: 12px; }}
h1 {{ font-size: 1.1rem; font-weight: 700; letter-spacing: -.01em; margin: 0;
  white-space: nowrap; }}
.top-right {{ display: flex; flex-wrap: wrap; align-items: center; gap: 10px; min-width: 0; }}
.updated {{ color: var(--muted); font-size: .78rem; text-align: right; line-height: 1.3; }}
.cog {{ flex: none; width: 40px; height: 40px; font-size: 1.15rem; line-height: 1; cursor: pointer;
  border: 1px solid var(--line); border-radius: 8px; background: var(--card); }}
form.filters {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 12px 0 4px; }}
form.filters input, form.filters select {{ width: 100%; min-width: 0; height: 42px; padding: 0 12px;
  border: 1px solid var(--line); border-radius: 8px; background: var(--card); color: var(--text); }}
form.filters input {{ grid-column: 1 / -1; }}
nav.tabs {{ display: flex; gap: 2px; overflow-x: auto; scrollbar-width: none; margin: 0 -16px;
  padding: 0 16px; -webkit-overflow-scrolling: touch; }}
nav.tabs::-webkit-scrollbar {{ display: none; }}
/* Fade at the right edge hints that the tab row scrolls. */
nav.tabs {{ mask-image: linear-gradient(to right, #000 calc(100% - 32px), transparent); }}
.tab {{ white-space: nowrap; padding: 10px 10px 9px; color: var(--muted); text-decoration: none;
  border-bottom: 2px solid transparent; font-size: .9rem; font-weight: 500; }}
.tab span {{ display: inline-block; min-width: 1.5em; margin-left: 2px; padding: 0 5px;
  border-radius: 999px; background: var(--line); font-size: .74rem; text-align: center; }}
.tab.active {{ color: var(--text); border-color: var(--accent); font-weight: 600; }}
.banner {{ background: var(--warn); color: #fff; padding: 8px 12px; border-radius: 8px;
  margin-top: 10px; font-size: .88rem; }}

/* Cards */
main {{ max-width: 792px; margin: 0 auto; padding: 12px 16px 48px; }}
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
.posted {{ white-space: nowrap; }}
.badges {{ display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }}
.badge {{ font-size: .7rem; font-weight: 600; text-transform: uppercase; letter-spacing: .04em;
  padding: 1px 7px; border-radius: 999px; border: 1px solid currentColor; }}
.g-exact {{ color: var(--exact); }} .g-near {{ color: var(--near); }}
.g-maybe {{ color: var(--maybe); }}
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

/* Wider screens */
@media (min-width: 640px) {{
  header {{ padding: 16px 24px 0; }}
  nav.tabs {{ margin: 0; padding: 0; flex-wrap: wrap; overflow: visible; mask-image: none; }}
  h1 {{ font-size: 1.25rem; }}
  .updated {{ font-size: .85rem; }}
  form.filters {{ grid-template-columns: 1fr 170px 170px; }}
  form.filters input {{ grid-column: auto; }}
  main {{ max-width: 808px; padding: 16px 24px 56px; }}
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
    <h1>Job Scraper</h1>
    <div class="top-right">
      <span class="updated">{updated}</span>
      <a class="btn" href="/companies">Tracked companies</a>
      <a class="btn" href="/logs">Crawl logs</a>
      <button type="button" class="btn" id="open-company">Add company to track</button>
      <button type="button" class="cog" id="open-settings" aria-label="Settings">⚙</button>
    </div>
  </div>
  {banner}
  <form class="filters" method="get">
    <input type="hidden" name="view" value="{view}">
    <input type="search" name="q" value="{q}" placeholder="Search title, employer, place">
    <select name="source" onchange="this.form.submit()">{source_opts}</select>
    <select name="sort" onchange="this.form.submit()">{sort_opts}</select>
  </form>
  <nav class="tabs">{tabs}</nav>
  </div>
</header>
<main>{cards}</main>
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
      key: card.dataset.key, field: btn.dataset.field, value,
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
function dropCard(card) {{
  const main = card.closest("main");
  card.classList.add("leaving");
  setTimeout(() => {{
    card.remove();
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
            elif url.path == "/companies":
                app.reload_config()
                self._send(
                    HTTPStatus.OK,
                    tracked.page(app.cfg, app.state(), stale_hours=STALE_HOURS),
                    "text/html",
                )
            elif url.path == "/logs":
                self._send(
                    HTTPStatus.OK,
                    activity.page(
                        [
                            app.state_dir / "crawl-history.json",
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
            if path not in ("/api/mark", "/api/employer", "/api/companies"):
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
                if path == "/api/companies":
                    entry = app.add_company(body)
                elif path == "/api/employer":
                    app.decide_employer(body["employer"], body["action"])
                    entry = {"ok": True}
                else:
                    entry = app.mark(body["key"], body["field"], body["value"])
                    query = {k: v[0] for k, v in parse_qs(url.query).items()}
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
