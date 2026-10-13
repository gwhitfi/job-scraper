import json
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from jobscraper import classify, state, userdata
from jobscraper.adapters import BOARD_ADAPTERS
from jobscraper.config import Config
from jobscraper.state import Posting, State

log = logging.getLogger(__name__)

NEW_DAYS = 2  # postings first seen this recently get a "new" badge

VIEWS = {
    "exact": "Exact",
    "near": "Near",
    "maybe": "Probably not",
    "relevant": "All matches",
    "starred": "Starred",
    "applied": "Applied",
    "hidden": "Hidden",
}
SOURCES = {"all": "All sources", "companies": "Company sites", "boards": "Job boards"}


@dataclass
class Row:
    posting: Posting
    group: str | None
    marks: dict
    source: str  # display label, e.g. "AFL (via Move Upstate SC)"
    is_board: bool


class App:
    def __init__(self, cfg: Config, state_dir: Path, marks_path: Path):
        self.cfg = cfg
        self.state_dir = state_dir
        self.marks_path = marks_path
        self.lock = threading.Lock()
        self._cache: tuple[float, State] | None = None
        self.companies = {c.id: c for c in cfg.companies}

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
        out = []
        for p in st.postings.values():
            company = self.companies.get(p.company_id)
            name = company.name if company else p.company_id
            out.append(
                Row(
                    posting=p,
                    group=classify.match_group(p.classification, self.cfg.profile),
                    marks=marks.get(p.key, {}),
                    source=f"{p.employer} (via {name})" if p.employer else name,
                    is_board=bool(company and company.adapter in BOARD_ADAPTERS),
                )
            )
        return out

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
        if hidden or not active:
            return False
        if view == "relevant":
            return r.group is not None
        return r.group == view

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
    def sort_key(r: Row):
        p = r.posting
        when = p.posted_date or p.first_seen.date()
        return (-when.toordinal(), p.title.lower())

    # --- pages ---

    def index(self, query: dict[str, str]) -> str:
        view = query.get("view") if query.get("view") in VIEWS else "exact"
        source = query.get("source") if query.get("source") in SOURCES else "all"
        q = query.get("q", "").strip()
        rows = self.rows()
        scoped = [r for r in rows if self.in_source(r, source) and self.matches_query(r, q)]
        counts = {v: sum(self.in_view(r, v) for r in scoped) for v in VIEWS}
        shown = sorted((r for r in scoped if self.in_view(r, view)), key=self.sort_key)
        return render_page(self, view, source, q, counts, shown)


# --- rendering ---

GROUP_LABELS = {"exact": "Exact", "near": "Near", "maybe": "Probably not"}


def _link(view: str, source: str, q: str) -> str:
    params = {"view": view, "source": source}
    if q:
        params["q"] = q
    return "/?" + urlencode(params)


def _health_banner(app: App) -> str:
    failing = [
        f"{escape(app.companies[cid].name if cid in app.companies else cid)}"
        f" ({h.consecutive_failures} failed checks)"
        for cid, h in app.state().health.items()
        if h.consecutive_failures >= 3
    ]
    if not failing:
        return ""
    return f'<div class="banner">Site checks failing: {", ".join(failing)}</div>'


def _updated(app: App) -> str:
    times = [h.last_success for h in app.state().health.values() if h.last_success]
    if not times:
        return "never updated"
    latest = max(times).astimezone()
    return f"updated {latest:%b %-d, %-I:%M %p}"


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
    when = f"posted {p.posted_date:%b %-d}" if p.posted_date else f"seen {p.first_seen:%b %-d}"
    level = c.get("level", "unknown")
    meta = " · ".join(
        escape(x) for x in (r.source, p.location, level if level != "unknown" else "", when) if x
    )
    reason = escape(c.get("reason", ""))
    note = escape(r.marks.get("note", ""))
    key = escape(p.key)

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
  <h2><a href="{escape(p.url)}" target="_blank" rel="noopener">{escape(p.title)}</a></h2>
  <p class="meta">{meta}</p>
  {f'<p class="reason">{reason}</p>' if reason else ""}
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


def render_page(
    app: App, view: str, source: str, q: str, counts: dict[str, int], shown: list[Row]
) -> str:
    today = datetime.now(UTC).date()
    tabs = "".join(
        f'<a class="tab{" active" if v == view else ""}" href="{escape(_link(v, source, q))}">'
        f"{label} <span>{counts[v]}</span></a>"
        for v, label in VIEWS.items()
    )
    source_opts = "".join(
        f'<option value="{s}"{" selected" if s == source else ""}>{label}</option>'
        for s, label in SOURCES.items()
    )
    cards = "".join(_card(r, today) for r in shown) or '<p class="empty">Nothing here.</p>'
    return PAGE.format(
        updated=escape(_updated(app)),
        banner=_health_banner(app),
        tabs=tabs,
        view=escape(view),
        source_opts=source_opts,
        q=escape(q),
        cards=cards,
    )


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Scraper</title>
<style>
:root {{
  --bg: #f6f7f9; --card: #fff; --text: #1b1f24; --muted: #5d6673; --line: #dde1e6;
  --accent: #2557d6; --exact: #1a7f37; --near: #9a6700; --maybe: #6e7781; --warn: #b42318;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #0f1216; --card: #171b21; --text: #e6e9ed; --muted: #9aa4b2; --line: #2a3038;
    --accent: #7aa2ff; --exact: #4ac26b; --near: #e3b341; --maybe: #9aa4b2; --warn: #ff7b72;
  }}
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--text);
  font: 16px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }}
header {{ position: sticky; top: 0; z-index: 1; background: var(--bg);
  border-bottom: 1px solid var(--line); padding: 12px 16px 0; }}
.top {{ display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }}
h1 {{ font-size: 1.15rem; margin: 0; }}
.updated {{ color: var(--muted); font-size: .85rem; }}
form.filters {{ display: flex; gap: 8px; margin: 10px 0; }}
form.filters input, form.filters select {{ font: inherit; padding: 6px 8px; min-width: 0;
  border: 1px solid var(--line); border-radius: 8px; background: var(--card); color: var(--text); }}
form.filters input {{ flex: 1; }}
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
.card h2 {{ font-size: 1.02rem; margin: 4px 0 2px; }}
.card h2 a {{ color: var(--text); text-decoration: none; }}
.card h2 a:hover {{ color: var(--accent); }}
.meta {{ color: var(--muted); font-size: .88rem; margin: 0; }}
.reason {{ font-size: .88rem; margin: 6px 0 0; }}
.badges {{ display: flex; gap: 6px; flex-wrap: wrap; }}
.badge {{ font-size: .72rem; font-weight: 600; text-transform: uppercase; letter-spacing: .03em;
  padding: 1px 6px; border-radius: 999px; border: 1px solid currentColor; }}
.g-exact {{ color: var(--exact); }} .g-near {{ color: var(--near); }}
.g-maybe {{ color: var(--maybe); }} .new {{ color: var(--accent); }}
.closed {{ color: var(--warn); }}
.actions {{ display: flex; gap: 6px; margin-top: 10px; flex-wrap: wrap; }}
.toggle {{ font: inherit; font-size: .88rem; padding: 6px 10px; border-radius: 8px; cursor: pointer;
  border: 1px solid var(--line); background: transparent; color: var(--text); }}
.toggle.on {{ background: var(--accent); border-color: var(--accent); color: #fff; }}
details {{ margin-top: 8px; font-size: .9rem; }}
summary {{ color: var(--muted); cursor: pointer; }}
textarea {{ width: 100%; margin-top: 6px; font: inherit; padding: 8px; border-radius: 8px;
  border: 1px solid var(--line); background: var(--bg); color: var(--text); }}
.saved {{ color: var(--muted); font-size: .8rem; }}
.empty {{ color: var(--muted); text-align: center; margin-top: 40px; }}
</style>
</head>
<body>
<header>
  <div class="top"><h1>Job Scraper</h1><span class="updated">{updated}</span></div>
  {banner}
  <form class="filters" method="get">
    <input type="hidden" name="view" value="{view}">
    <input type="search" name="q" value="{q}" placeholder="Search title, employer, place">
    <select name="source" onchange="this.form.submit()">{source_opts}</select>
  </form>
  <nav class="tabs">{tabs}</nav>
</header>
<main>{cards}</main>
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
  const btn = e.target.closest(".toggle");
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
            if urlparse(self.path).path != "/api/mark":
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
                entry = app.mark(body["key"], body["field"], body["value"])
            except KeyError:
                self._send(HTTPStatus.NOT_FOUND, "Unknown posting", "text/plain")
                return
            except (ValueError, TypeError) as e:
                self._send(HTTPStatus.BAD_REQUEST, str(e), "text/plain")
                return
            self._send(HTTPStatus.OK, json.dumps(entry), "application/json")

        def log_message(self, fmt: str, *args) -> None:
            log.info("%s %s", self.address_string(), fmt % args)

    return ThreadingHTTPServer((host, port), Handler)
