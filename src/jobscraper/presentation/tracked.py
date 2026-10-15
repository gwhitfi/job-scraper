"""Company status overview based on published crawl health."""

from datetime import UTC, datetime, timedelta
from html import escape

from jobscraper import activity
from jobscraper.config import Config
from jobscraper.models import Company
from jobscraper.storage.state import Health, State
from jobscraper.timezone import EASTERN
from jobscraper.workflow import notify
from jobscraper.workflow.tracker import FAILURES_BEFORE_ALERT


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
        url = activity.safe_url(company.url)
        rows.append(f'''<tr data-company="{escape(company.id, quote=True)}">
          <th scope="row"><a href="{escape(notify.safe_url(company.url), quote=True)}"
             target="_blank" rel="noopener noreferrer">{escape(names[company.id])}</a>
             <small>{escape(url)}</small></th>
          <td><span class="status {kind}"><span aria-hidden="true">{symbol}</span> {label}</span>
              <details><summary>Details</summary><p>{escape(activity.clean(detail))}</p></details></td>
          <td>{count}</td><td>{last}</td></tr>''')
    total = sum(c.enabled for c in cfg.companies)
    body = "".join(rows) or '<tr><td colspan="4">No companies are configured yet.</td></tr>'
    return (
        """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tracked companies · Job Scraper</title><style>
:root{color-scheme:light dark;--bg:#f4f5f7;--card:#fff;--text:#16191d;--muted:#5b6470;
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
</style></head><body><main><nav><a href="/">← Jobs / Add company</a>
<a href="/logs">Crawl logs</a></nav><h1>Tracked companies</h1>"""
        + f"""
<p class="intro">{total} enabled source(s). Status reflects the latest published scheduled checks.
Refresh to see updates. Job counts are from the last successful crawl, after location filtering.</p>
<div class="legend"><span class="good">✓ Good: {counts["good"]}</span>
<span class="warning">⚠ Needs attention: {counts["warning"]}</span>
<span class="bad">✕ Failing: {counts["bad"]}</span>
<span class="muted">○ Waiting / paused: {counts["muted"]}</span></div>
<div class="table-wrap"><table><thead><tr><th>Company / source</th><th>Status</th>
<th>Jobs</th><th>Last successful crawl</th></tr></thead><tbody>{body}</tbody></table></div>
<p class="intro">Amber means a crawl recovered after retries, a previously successful source has
1–2 failed checks, or a check is overdue. Red means {FAILURES_BEFORE_ALERT} consecutive failures
or a failed source with no successful crawl yet.</p></main></body></html>"""
    )
