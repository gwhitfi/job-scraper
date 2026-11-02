"""Company status overview based on published crawl health."""

from collections import Counter
from datetime import UTC, datetime, timedelta
from html import escape
from urllib.parse import urlencode

from jobscraper import activity
from jobscraper.config import Config
from jobscraper.models import Company
from jobscraper.storage.state import Health, State
from jobscraper.timezone import EASTERN
from jobscraper.workflow import notify
from jobscraper.workflow.tracker import FAILURES_BEFORE_ALERT

STYLE = """:root{color-scheme:light dark;--bg:#f4f5f7;--card:#fff;--text:#16191d;--muted:#5b6470;
--line:#e1e4e8;--link:#2557d6;--good:#1a7f37;--warning:#9a6700;--bad:#b42318}
@media(prefers-color-scheme:dark){:root{--bg:#0e1115;--card:#161a20;--text:#e6e9ed;
--muted:#98a2b0;--line:#262c34;--link:#7aa2ff;--good:#4ac26b;--warning:#e3b341;--bad:#ff7b72}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:16px/1.5 system-ui,sans-serif}main{max-width:1200px;margin:2rem auto;padding:0 1rem}
nav{display:flex;gap:1.5rem;flex-wrap:wrap}a{color:var(--link)}h1{margin-bottom:.4rem}
.intro,small{color:var(--muted)}.table-wrap{overflow:auto;background:var(--card);
border:1px solid var(--line);border-radius:10px}table{border-collapse:collapse;width:100%}
th,td{padding:1rem;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
thead th{white-space:nowrap}tbody th{min-width:230px;max-width:400px}small{display:block;
font-size:.8rem;font-weight:normal;overflow-wrap:anywhere}.status{font-weight:600;white-space:nowrap}
.good{color:var(--good)}.warning{color:var(--warning)}.bad{color:var(--bad)}.muted{color:var(--muted)}
summary{cursor:pointer;font-size:.85rem;color:var(--muted);margin-top:.5rem}
details p{max-width:400px;min-width:180px;overflow-wrap:anywhere;font-size:.85rem}
.legend{display:flex;gap:1rem;flex-wrap:wrap;margin:1rem 0}a:focus-visible,summary:focus-visible{
outline:2px solid var(--link);outline-offset:3px}
button,input{font:inherit}button{cursor:pointer}.edit-name{margin-left:.5rem;font-size:.8rem}
.name-form{font-weight:normal}.name-form label{display:block}
.name-error{display:block;color:var(--bad)}
button:focus-visible,input:focus-visible{outline:2px solid var(--link);outline-offset:3px}

.toolbar{display:flex;gap:1rem;flex-wrap:wrap;align-items:end;margin:1.5rem 0}
.toolbar label{display:grid;gap:.3rem}.toolbar input,.toolbar select,.toolbar button{
background:var(--card);color:var(--text);border:1px solid var(--line);border-radius:6px;
padding:.65rem}
.job-count{display:inline-block;font-weight:700;padding:.5rem .8rem;border:1px solid var(--line);
border-radius:6px;white-space:nowrap;text-decoration:none}.job-count:hover{background:var(--bg)}
tbody tr:hover{background:var(--bg)}select{font:inherit}.empty{padding:2rem;text-align:center}
"""


def status(
    company: Company, health: Health | None, now: datetime, stale_hours: int
) -> tuple[str, str, str]:
    if not company.enabled:
        return "muted", "Paused", "Tracking is disabled."
    if health and health.consecutive_failures:
        failing = health.consecutive_failures >= FAILURES_BEFORE_ALERT or not health.last_success
        return (
            "bad" if failing else "warning",
            "Failing to crawl" if failing else "Crawling with errors",
            f"{health.consecutive_failures} consecutive failed check(s). "
            + (health.last_error or "No error details recorded."),
        )
    if not health or not health.last_success:
        return (
            "muted",
            "Waiting for first crawl",
            "Setup is complete; waiting for a scheduled check.",
        )
    if now - health.last_success > timedelta(hours=stale_hours):
        return "warning", "Check overdue", f"No successful crawl in over {stale_hours} hours."
    if health.last_warning:
        return "warning", "Crawling with errors", health.last_warning
    return "good", "Good", "Latest crawl completed successfully."


def page(cfg: Config, state: State, *, stale_hours: int, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    names = cfg.health_names()
    active_counts = Counter(p.company_id for p in state.postings.values() if p.status == "active")
    rows = []
    counts = {"good": 0, "warning": 0, "bad": 0, "muted": 0}
    for company in sorted(cfg.companies, key=lambda c: (names[c.id].casefold(), c.id)):
        health = state.health.get(company.id)
        kind, label, detail = status(company, health, now, stale_hours)
        counts[kind] += 1
        symbol = {"good": "✓", "warning": "⚠", "bad": "✕", "muted": "○"}[kind]
        last = (
            health.last_success.astimezone(EASTERN).strftime("%b %d, %Y %-I:%M %p %Z")
            if health and health.last_success
            else "Never"
        )
        count = str(health.last_count) if health and health.last_count is not None else "—"
        href = escape("/companies?" + urlencode({"company": company.id}))
        url = activity.safe_url(company.url)
        rows.append(f'''<tr data-company="{escape(company.id, quote=True)}" data-status="{kind}">
          <th scope="row"><a href="{escape(notify.safe_url(company.url), quote=True)}"
             target="_blank" rel="noopener noreferrer">{escape(names[company.id])}</a>
             <button type="button" class="edit-name">Edit name</button>
             <form class="name-form" hidden>
               <label>Company name <input name="name" maxlength="200" required
                 value="{escape(company.name, quote=True)}"></label>
               <button type="submit">Save</button>
               <button type="button" class="cancel-name">Cancel</button>
               <span class="name-error" role="alert"></span>
             </form><small>{escape(url)}</small></th>
          <td><span class="status {kind}"><span aria-hidden="true">{symbol}</span> {label}</span>
              <details><summary>Details</summary><p>{escape(activity.clean(detail))}</p></details></td>
          <td><a class="job-count" href="{href}"
              aria-label="View all {active_counts[company.id]} active jobs at
{escape(names[company.id], quote=True)}">{active_counts[company.id]} jobs →</a>
              <small>Last crawl: {count}</small></td><td>{last}</td></tr>''')
    total = sum(c.enabled for c in cfg.companies)
    body = "".join(rows) or '<tr><td colspan="4">No companies are configured yet.</td></tr>'
    return (
        """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tracked companies · Job Scraper</title><style>
"""
        + STYLE
        + """</style></head><body><main><nav><a href="/">← Jobs / Add company</a>
<a href="/logs">Crawl logs</a></nav><h1>Tracked companies</h1>"""
        + f"""
<p class="intro">{total} enabled source(s). Status reflects the latest published scheduled checks.
Click a job count to browse every active posting saved for that source.
Last crawl counts may differ while missing postings await confirmation of removal.</p>
<div class="legend"><span class="good">✓ Good: {counts["good"]}</span>
<span class="warning">⚠ Needs attention: {counts["warning"]}</span>
<span class="bad">✕ Failing: {counts["bad"]}</span>
<span class="muted">○ Waiting / paused: {counts["muted"]}</span></div>
<form class="toolbar" id="company-filters" role="search">
<label>Find a company<input type="search" id="company-search"
placeholder="Company name or website"></label>
<label>Crawl status<select id="company-status"><option value="">All statuses</option>
<option value="good">Good</option><option value="warning">Needs attention</option>
<option value="bad">Failing</option><option value="muted">Waiting / paused</option></select></label>
<button type="reset">Clear filters</button></form>
<p id="company-results" role="status">{len(rows)} companies</p>
<div class="table-wrap"><table><thead><tr><th>Company / source</th><th>Status</th>
<th>Active jobs</th><th>Last successful crawl</th></tr></thead><tbody>{body}</tbody></table>
<p class="empty" id="no-companies" hidden>No companies match.
Try another name or clear the filters.</p></div>
<p class="intro">Amber means a crawl recovered after retries, a previously successful source has
1–2 failed checks, or a check is overdue. Red means {FAILURES_BEFORE_ALERT} consecutive failures
or a failed source with no successful crawl yet.</p></main>
<script>
const filters = document.querySelector('#company-filters');
function filterCompanies() {{
  const q = document.querySelector('#company-search').value.trim().toLowerCase();
  const status = document.querySelector('#company-status').value;
  let visible = 0;
  document.querySelectorAll('tr[data-company]').forEach(row => {{
    row.hidden = !(row.querySelector('th').textContent.toLowerCase().includes(q)
      && (!status || row.dataset.status === status));
    if (!row.hidden) visible++;
  }});
  document.querySelector('#company-results').textContent = visible + ' companies';
  document.querySelector('#no-companies').hidden = visible !== 0;
}}
filters.addEventListener('input', filterCompanies);
filters.addEventListener('submit', event => event.preventDefault());
filters.addEventListener('reset', () => setTimeout(filterCompanies, 0));
document.querySelectorAll('tr[data-company]').forEach(row => {{
  const edit = row.querySelector('.edit-name');
  const form = row.querySelector('.name-form');
  const input = form.elements.name;
  const error = form.querySelector('.name-error');
  let saved = input.value;
  function close() {{
    input.value = saved; error.textContent = ''; form.hidden = true;
    edit.hidden = false; edit.focus();
  }}
  edit.addEventListener('click', () => {{
    edit.hidden = true; form.hidden = false; input.focus(); input.select();
  }});
  form.querySelector('.cancel-name').addEventListener('click', close);
  form.addEventListener('keydown', event => {{
    if (event.key === 'Escape' && !input.disabled) {{ event.preventDefault(); close(); }}
  }});
  form.addEventListener('submit', async event => {{
    event.preventDefault(); error.textContent = '';
    const buttons = form.querySelectorAll('button');
    buttons.forEach(button => button.disabled = true);
    input.disabled = true;
    try {{
      const response = await fetch('/api/company-name', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json', 'X-Requested-With': 'jobscraper'}},
        body: JSON.stringify({{company_id: row.dataset.company, name: input.value}})
      }});
      if (!response.ok) throw new Error(await response.text());
      const result = await response.json();
      saved = result.name; row.querySelector('th a').textContent = saved; close();
    }} catch (failure) {{ error.textContent = failure.message; }}
    finally {{ buttons.forEach(button => button.disabled = false); input.disabled = false; }}
  }});
}});
</script></body></html>"""
    )


def jobs_page(cfg: Config, state: State, query: dict[str, str], *, stale_hours: int) -> str:
    company = next(c for c in cfg.companies if c.id == query["company"])
    name = escape(cfg.health_names()[company.id])
    q = query.get("q", "").strip()
    sort = query.get("sort", "newest")
    active = [
        p for p in state.postings.values() if p.company_id == company.id and p.status == "active"
    ]
    shown = [p for p in active if q.casefold() in f"{p.title} {p.location} {p.employer}".casefold()]
    shown.sort(
        key=lambda p: (
            (p.title.casefold(), p.key)
            if sort == "title"
            else (-p.first_seen.timestamp(), p.title.casefold(), p.key)
        )
    )
    rows = []
    for p in shown:
        notes = []
        if p.missed_runs:
            notes.append("Missing from latest crawl · awaiting confirmation")
        if p.duplicate_of:
            notes.append("Also tracked through another source")
        if not p.classification:
            notes.append("Not yet classified")
        rows.append(f'''<tr data-job="{escape(p.key, quote=True)}">
<th scope="row"><a href="{escape(notify.safe_url(p.url), quote=True)}"
 target="_blank" rel="noopener noreferrer">{escape(p.title)} ↗</a>
<small>{escape(p.employer)}</small><small>{escape(" · ".join(notes))}</small></th>
<td>{escape(p.location) or "Location not provided"}</td>
<td>{p.first_seen.astimezone(EASTERN).strftime("%b %d, %Y")}</td>
<td>{p.last_seen.astimezone(EASTERN).strftime("%b %d, %Y")}</td></tr>''')
    health = state.health.get(company.id)
    kind, label, detail = status(company, health, datetime.now(UTC), stale_hours)
    last = (
        health.last_success.astimezone(EASTERN).strftime("%b %d, %Y %-I:%M %p %Z")
        if health and health.last_success
        else "Never"
    )
    crawl_count = health.last_count if health and health.last_count is not None else "—"
    clear = escape("/companies?" + urlencode({"company": company.id}))
    options = "".join(
        f'<option value="{value}"{" selected" if sort == value else ""}>{label}</option>'
        for value, label in [("newest", "Newest added"), ("title", "Job title A–Z")]
    )
    empty = (
        "No jobs match your search. Try a different title or location."
        if active
        else "No active postings saved for this source yet. "
        "Check its crawl status above or visit its careers site."
    )
    body = "".join(rows) or f'<tr><td colspan="4" class="empty">{empty}</td></tr>'
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name} jobs · Job Scraper</title><style>{STYLE}</style></head><body><main>
<nav><a href="/companies">← All companies</a><a href="/">Job dashboard</a>
<a href="{escape(notify.safe_url(company.url), quote=True)}" target="_blank"
 rel="noopener noreferrer">Company careers site ↗</a></nav>
<h1>{name} jobs</h1><p><strong>{len(active)} active postings</strong> saved for this source.</p>
<p class="intro">All active postings are included, regardless of relevance, hidden status,
applications, or duplicates. Removed postings are excluded.</p>
<p><span class="status {kind}">{label}</span> · Last successful crawl: {last}
 · Last crawl: {crawl_count} jobs</p><details><summary>Crawl details and counts</summary>
<p>{escape(activity.clean(detail))}</p><p>Active totals can differ from the last crawl:
missing jobs stay active until removal is confirmed by consecutive successful checks.</p></details>
<form class="toolbar" action="/companies" method="get" role="search">
<input type="hidden" name="company" value="{escape(company.id, quote=True)}">
<label>Search jobs<input type="search" name="q" value="{escape(q, quote=True)}"
 placeholder="Job title, location, or employer"></label>
<label>Sort by<select name="sort">{options}</select></label>
<button type="submit">Apply</button><a href="{clear}">Clear filters</a></form>
<p role="status">Showing {len(shown)} of {len(active)} active postings</p>
<div class="table-wrap"><table><thead><tr><th>Job / employer</th><th>Location</th>
<th>First seen</th><th>Last seen</th></tr></thead><tbody>{body}</tbody></table></div>
</main></body></html>'''
