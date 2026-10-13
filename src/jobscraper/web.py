import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlencode, urlparse

from jobscraper import classify, distance, employers, jsonfile, notify, state, usage, userdata
from jobscraper import config as config_file
from jobscraper.config import Config
from jobscraper.state import Posting, State

log = logging.getLogger(__name__)

NEW_DAYS = 2  # postings first seen this recently get a "new" badge
STALE_HOURS = 6  # no successful check for this long means the runner (or my PC) is probably off
# Matches the cron in .github/workflows/check.yml ("17 */2 * * *", UTC); a test keeps them in sync.
SCHEDULE_MINUTE = 17
SCHEDULE_EVERY_HOURS = 2

VIEWS = {
    "exact": "Exact",
    "near": "Near",
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
        self.config_path = config_path  # reloaded when it changes, if given
        self._config_mtime = config_path.stat().st_mtime if config_path else None
        self.lock = threading.Lock()
        self._cache: tuple[float, State] | None = None
        self._use_config(cfg)

    def _use_config(self, cfg: Config) -> None:
        self.cfg = cfg
        self.companies = {c.id: c for c in cfg.companies}
        self.home = distance.home_coords(cfg.web.home)

    def reload_config(self) -> None:
        """Pick up a new config.toml (e.g. the copy each run publishes) without a restart."""
        if self.config_path is None or not self.config_path.exists():
            return
        mtime = self.config_path.stat().st_mtime
        if mtime == self._config_mtime:
            return
        try:
            self._use_config(config_file.load(self.config_path))
        except Exception:
            log.exception("Couldn't reload %s; keeping the previous config", self.config_path)
        self._config_mtime = mtime

    # --- data ---

    def state(self) -> State:
        """Reload only when the runner has written new state."""
        path = self.state_dir / "postings.json"
        mtime = path.stat().st_mtime if path.exists() else 0.0
        if self._cache is None or self._cache[0] != mtime:
            self._cache = (mtime, state.load(self.state_dir))
        return self._cache[1]

    def rows(self) -> list[Row]:
        st = self.state()
        marks = userdata.load(self.marks_path)
        decisions = employers.load(self.employers_path)
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
        rows = [r for r in self.rows() if r.is_board and r.employer_key == key]
        if not rows:
            raise KeyError(key)
        with self.lock:
            decisions = employers.load(self.employers_path)
            name = rows[0].posting.employer or rows[0].posting.title
            employers.decide(decisions, key, name, EMPLOYER_ACTIONS[action])
            employers.save(self.employers_path, decisions)

    def mark(self, key: str, field: str, value) -> dict:
        if key not in self.state().postings:
            raise KeyError(key)
        with self.lock:
            marks = userdata.load(self.marks_path)
            entry = userdata.update(marks, key, field, value)
            userdata.save(self.marks_path, marks)
        return entry

    # --- filtering ---

    @staticmethod
    def in_view(r: Row, view: str) -> bool:
        active = r.posting.status == "active"
        hidden = r.marks.get("hidden", False)
        if view == "hidden":
            return hidden
        if view == "starred":
            return r.marks.get("starred", False) and not hidden
        if view == "applied":
            return r.marks.get("applied", False)
        if view == "blocked":
            return active and r.group is not None and r.employer_status == "blocked"
        if hidden or not active or r.group is None:
            return False
        if view == "unverified":
            return r.employer_status == "unverified"
        if r.employer_status in ("unverified", "blocked"):
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
        when = (p.posted_date or p.first_seen.date()).toordinal()
        if sort == "relevant":
            # Keyword-only results have no score yet; they sort last.
            score = (p.classification or {}).get("score") or 0
            return (-score, -when, p.title.lower())
        if sort == "nearest":
            # Remote and unknown places sort last.
            return (r.miles is None, r.miles or 0, -when, p.title.lower())
        return (-when, p.title.lower())

    # --- pages ---

    def index(self, query: dict[str, str]) -> str:
        self.reload_config()
        view = query.get("view") if query.get("view") in VIEWS else "exact"
        source = query.get("source") if query.get("source") in SOURCES else "all"
        sort = query.get("sort") if query.get("sort") in SORTS else "newest"
        q = query.get("q", "").strip()
        rows = self.rows()
        scoped = [r for r in rows if self.in_source(r, source) and self.matches_query(r, q)]
        counts = {v: sum(self.in_view(r, v) for r in scoped) for v in VIEWS}
        shown = sorted(
            (r for r in scoped if self.in_view(r, view)), key=lambda r: self.sort_key(r, sort)
        )
        return render_page(self, view, source, q, sort, counts, shown)


# --- rendering ---

GROUP_LABELS = {"exact": "Exact", "near": "Near", "maybe": "Probably not"}

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
        if h.consecutive_failures >= 3
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


def _card(r: Row, today: date) -> str:
    p = r.posting
    c = p.classification or {}
    badges = []
    if r.group:
        badges.append(f'<span class="badge g-{r.group}">{GROUP_LABELS[r.group]}</span>')
    if p.first_seen.date() >= today - timedelta(days=NEW_DAYS) and p.status == "active":
        badges.append('<span class="badge new">New</span>')
    if p.status == "removed":
        badges.append('<span class="badge closed">Closed</span>')
    if p.area == "extended":
        badges.append('<span class="badge outside">Outside Upstate</span>')
    score = c.get("score")
    if score:
        badges.append(f'<span class="score" title="Similarity to target role">{score}/10</span>')
    when = f"posted {p.posted_date:%b %-d}" if p.posted_date else f"seen {p.first_seen:%b %-d}"
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
<article class="card" data-key="{key}">
  <div class="badges">{"".join(badges)}</div>
  <p class="company">{escape(r.source)}</p>
  <h2><a href="{href}" target="_blank" rel="noopener">{escape(p.title)}</a></h2>
  <p class="meta">{meta}</p>
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
    today = datetime.now(UTC).date()
    tabs = "".join(
        f'<a class="tab{" active" if v == view else ""}" '
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
    cards = "".join(_card(r, today) for r in shown) or '<p class="empty">Nothing here.</p>'
    return PAGE.format(
        head_script=HEAD_SCRIPT,
        font_head=_font_select("head", "Company &amp; position"),
        font_body=_font_select("body", "Rest of page"),
        updated=escape(_updated(app)),
        banner=_banners(app, datetime.now(UTC)),
        tabs=tabs,
        view=escape(view),
        source_opts=source_opts,
        sort_opts=sort_opts,
        q=escape(q),
        cards=cards,
        footer=escape(
            usage.summary(jsonfile.read(app.state_dir / "usage.json"), datetime.now(UTC))
        ),
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
  --bg: #f6f7f9; --card: #fff; --text: #1b1f24; --muted: #5d6673; --line: #dde1e6;
  --accent: #2557d6; --exact: #1a7f37; --near: #9a6700; --maybe: #6e7781; --warn: #b42318;
  --company: #0b5cad; --title: #9a4a00;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #0f1216; --card: #171b21; --text: #e6e9ed; --muted: #9aa4b2; --line: #2a3038;
    --accent: #7aa2ff; --exact: #4ac26b; --near: #e3b341; --maybe: #9aa4b2; --warn: #ff7b72;
    --company: #79c0ff; --title: #ffcf70;
  }}
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--text); font-size: 16px; line-height: 1.45;
  font-family: var(--font-body, system-ui, -apple-system, "Segoe UI", sans-serif); }}
button, input, select, textarea {{ font-family: inherit; }}
header {{ position: sticky; top: 0; z-index: 1; background: var(--bg);
  border-bottom: 1px solid var(--line); padding: 12px 16px 0; }}
.top {{ display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }}
h1 {{ font-size: 1.15rem; margin: 0; }}
.updated {{ color: var(--muted); font-size: .85rem; }}
form.filters {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0; }}
form.filters input, form.filters select {{ font: inherit; padding: 6px 8px; min-width: 0;
  border: 1px solid var(--line); border-radius: 8px; background: var(--card); color: var(--text); }}
form.filters input {{ flex: 1 1 100%; }}
form.filters select {{ flex: 1; }}
nav.tabs {{ display: flex; gap: 4px; overflow-x: auto; scrollbar-width: none; }}
.tab {{ white-space: nowrap; padding: 8px 10px; color: var(--muted); text-decoration: none;
  border-bottom: 2px solid transparent; font-size: .92rem; }}
.tab span {{ font-size: .8rem; opacity: .8; }}
.tab.active {{ color: var(--text); border-color: var(--accent); font-weight: 600; }}
main {{ max-width: 760px; margin: 0 auto; padding: 12px 16px 48px; }}
.banner {{ background: var(--warn); color: #fff; padding: 8px 12px; border-radius: 8px;
  margin-top: 8px; font-size: .9rem; }}
.card {{ background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  padding: 12px 14px; margin-bottom: 10px; }}
.company, .card h2 {{ font-family: var(--font-head, inherit); font-weight: 700; }}
.company {{ font-size: 17px; margin: 6px 0 0; color: var(--company); }}
.card h2 {{ font-size: 15px; margin: 2px 0; }}
.card h2 a {{ color: var(--title); text-decoration: none; }}
.card h2 a:hover {{ color: var(--accent); }}
.meta {{ color: var(--muted); font-size: .88rem; margin: 0; }}
.posted {{ text-decoration: underline; text-underline-offset: 2px; }}
.reason {{ font-size: .88rem; margin: 6px 0 0; }}
.badges {{ display: flex; gap: 6px; flex-wrap: wrap; }}
.badge {{ font-size: .72rem; font-weight: 600; text-transform: uppercase; letter-spacing: .03em;
  padding: 1px 6px; border-radius: 999px; border: 1px solid currentColor; }}
.g-exact {{ color: var(--exact); }} .g-near {{ color: var(--near); }}
.g-maybe {{ color: var(--maybe); }} .new {{ color: var(--accent); }}
.closed {{ color: var(--warn); }} .outside {{ color: var(--muted); }}
.score {{ margin-left: auto; font-size: .78rem; font-weight: 700; color: var(--muted); }}
.actions {{ display: flex; gap: 6px; margin-top: 10px; flex-wrap: wrap; }}
.toggle {{ font: inherit; font-size: .88rem; padding: 6px 10px; border-radius: 8px; cursor: pointer;
  border: 1px solid var(--line); background: transparent; color: var(--text); }}
.toggle.on {{ background: var(--accent); border-color: var(--accent); color: #fff; }}
.btn {{ font: inherit; font-size: .9rem; padding: 6px 14px; border-radius: 8px; cursor: pointer;
  border: 1px solid var(--line); background: transparent; color: var(--text); }}
.btn.primary {{ background: var(--accent); border-color: var(--accent); color: #fff; }}
details {{ margin-top: 8px; font-size: .9rem; }}
summary {{ color: var(--muted); cursor: pointer; }}
textarea {{ width: 100%; margin-top: 6px; font: inherit; padding: 8px; border-radius: 8px;
  border: 1px solid var(--line); background: var(--bg); color: var(--text); }}
.saved {{ color: var(--muted); font-size: .8rem; }}
.empty {{ color: var(--muted); text-align: center; margin-top: 40px; }}
.top-right {{ display: flex; align-items: center; gap: 8px; }}
.cog {{ font-size: 1.2rem; line-height: 1; padding: 4px 6px; border-radius: 8px; cursor: pointer;
  border: 1px solid var(--line); background: transparent; color: var(--text); }}
dialog {{ border: 1px solid var(--line); border-radius: 12px; background: var(--card);
  color: var(--text); width: min(92vw, 380px); padding: 16px; }}
dialog::backdrop {{ background: rgb(0 0 0 / .45); }}
dialog h2 {{ margin: 0 0 12px; font-size: 1.05rem; }}
dialog label {{ display: block; font-size: .9rem; color: var(--muted); margin-bottom: 12px; }}
dialog select {{ display: block; width: 100%; margin-top: 4px; font-size: 1rem; padding: 6px 8px;
  border: 1px solid var(--line); border-radius: 8px; background: var(--bg); color: var(--text); }}
.also-on {{ font-size: .82rem; color: var(--muted); margin: 2px 0 0; }}
.also-on a {{ color: var(--accent); }}
.employer-panel {{ margin-top: 10px; padding: 8px 10px; border-radius: 8px;
  border: 1px dashed var(--near); font-size: .88rem; }}
.employer-panel p {{ margin: 0 0 4px; }}
.verify-links a {{ color: var(--accent); }}
.employer-panel .actions {{ margin-top: 6px; }}
footer {{ max-width: 760px; margin: 0 auto; padding: 0 16px 32px; color: var(--muted);
  font-size: .8rem; text-align: center; }}
.dialog-actions {{ display: flex; justify-content: space-between; gap: 8px; }}
</style>
</head>
<body>
<header>
  <div class="top">
    <h1>Job Scraper</h1>
    <div class="top-right">
      <span class="updated">{updated}</span>
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
</header>
<main>{cards}</main>
<footer>{footer}</footer>
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
async function mark(card, field, value) {{
  const res = await fetch("/api/mark", {{
    method: "POST",
    headers: {{"Content-Type": "application/json", "X-Requested-With": "jobscraper"}},
    body: JSON.stringify({{key: card.dataset.key, field, value}}),
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
    await mark(card, btn.dataset.field, value);
    btn.setAttribute("aria-pressed", String(value));
    btn.classList.toggle("on", value);
    btn.textContent = value ? btn.dataset.on : btn.dataset.off;
  }} catch (err) {{
    alert("Couldn't save: " + err.message);
  }} finally {{
    btn.disabled = false;
  }}
}});
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
    const res = await fetch("/api/employer", {{
      method: "POST",
      headers: {{"Content-Type": "application/json", "X-Requested-With": "jobscraper"}},
      body: JSON.stringify({{employer: btn.dataset.employer, action: btn.dataset.action}}),
    }});
    if (!res.ok) throw new Error(await res.text());
    location.reload();  // every posting from this employer moves tabs
  }} catch (err) {{
    alert("Couldn't save: " + err.message);
    btn.disabled = false;
  }}
}});
document.addEventListener("change", async (e) => {{
  if (e.target.tagName !== "TEXTAREA") return;
  const card = e.target.closest(".card");
  const status = card.querySelector(".saved");
  try {{
    await mark(card, "note", e.target.value);
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
            if url.path != "/":
                self._send(HTTPStatus.NOT_FOUND, "Not found", "text/plain")
                return
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            self._send(HTTPStatus.OK, app.index(query), "text/html")

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path not in ("/api/mark", "/api/employer"):
                self._send(HTTPStatus.NOT_FOUND, "Not found", "text/plain")
                return
            # A custom header can't be sent cross-site without a CORS preflight, which this
            # server never approves, so other sites can't change my marks.
            if self.headers.get("X-Requested-With") != "jobscraper":
                self._send(HTTPStatus.FORBIDDEN, "Forbidden", "text/plain")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(min(length, 64_000)))
                if path == "/api/employer":
                    app.decide_employer(body["employer"], body["action"])
                    entry = {"ok": True}
                else:
                    entry = app.mark(body["key"], body["field"], body["value"])
            except KeyError:
                self._send(HTTPStatus.NOT_FOUND, "Unknown posting or employer", "text/plain")
                return
            except (ValueError, TypeError) as e:
                self._send(HTTPStatus.BAD_REQUEST, str(e), "text/plain")
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
