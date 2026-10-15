"""Readable, accessible presentation of saved activity traces."""

import json
from datetime import UTC, datetime
from html import escape

from jobscraper.timezone import EASTERN


def stamp(value: str, short: bool = False) -> str:
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        dt = dt.astimezone(EASTERN)
        return dt.strftime("%-I:%M:%S %p %Z" if short else "%b %d, %Y · %-I:%M %p %Z")
    except ValueError:
        return value


def duration(value) -> str:
    if value is None:
        return "In progress"
    seconds = round(float(value))
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def tone(event: dict) -> str:
    stage = event["stage"]
    if event.get("status") == "failed":
        return "error"
    if event.get("status") == "partial_failure":
        return "warning"
    if event.get("error") or "failed" in stage or stage == "error":
        return "error"
    if event.get("status_code", 0) >= 400:
        return "error"
    if event.get("warning") or stage in ("retry", "classification_fallback"):
        return "warning"
    if stage.endswith("_completed") or stage == "finished":
        return "success"
    return "info"


def badge(label: str, color: str) -> str:
    return f'<span class="badge {color}">{escape(label)}</span>'


def render_record(r: dict, selected: str) -> str:
    rid = escape(r["id"], quote=True)
    events = r["events"]
    names = {
        e["company"]: e.get("name", e["company"]) for e in events if e["stage"] == "company_started"
    }
    companies = [e for e in events if e["stage"] in ("company_completed", "company_failed")]
    failed = [e for e in companies if e["stage"] == "company_failed"]
    status = r["status"]
    labels = {
        "completed": ("Completed", "success"),
        "failed": ("Failed", "error"),
        "partial_failure": ("Completed with issues", "warning"),
        "running": ("In progress", "info"),
    }
    label, color = labels.get(status, (status.replace("_", " ").title(), "info"))
    if status == "completed" and failed:
        label, color = "Completed with issues", "warning"
    kind = "Scheduled crawl" if r["kind"] == "crawl" else "Add company"
    opened = " open" if r["id"] == selected else ""
    parts = [
        f'<details class="run" id="{rid}"{opened}><summary>',
        f'<span class="run-title">{escape(kind)}<small>{escape(stamp(r["started"]))}</small>'
        f'</span>{badge(label, color)}</summary><div class="run-body">',
    ]
    requests = sum(e["stage"] == "request" for e in events)
    http_errors = sum(e["stage"] == "response" and e.get("status_code", 0) >= 400 for e in events)
    stats = [
        (duration(r.get("seconds")), "Duration", "info"),
        (str(len(companies) - len(failed)), "Sources completed", "success"),
        (str(len(failed)), "Sources failed", "error" if failed else "muted"),
        (str(requests), "Requests", "info"),
        (str(http_errors), "HTTP errors", "error" if http_errors else "muted"),
    ]
    parts.append(
        '<div class="metrics">'
        + "".join(
            f'<div class="metric {c}"><strong>{v}</strong><span>{name}</span></div>'
            for v, name, c in stats
        )
        + "</div>"
    )
    if status == "running":
        last = stamp(events[-1]["time"]) if events else stamp(r["started"])
        parts.append(
            f'<p class="notice">Last activity: {escape(last)}. Refresh for progress. '
            "An entry without recent activity may have been interrupted.</p>"
        )
    if r.get("omitted_events"):
        parts.append(
            f'<p class="notice">{int(r["omitted_events"])} older events omitted '
            "by the size limit. Counts reflect the retained events.</p>"
        )
    issues = failed or [e for e in events if e["stage"] == "error"]
    if issues:
        parts.append('<section class="issues"><h2>Needs attention</h2>')
        for e in issues:
            company = names.get(e.get("company"), e.get("company", "Run error"))
            parts.append(
                f'<div class="issue"><strong>{escape(company)}</strong>'
                f"<p>{escape(str(e.get('error', 'Source failed')))}</p></div>"
            )
        parts.append("</section>")
    progress = next(
        (
            e
            for e in reversed(events)
            if e["stage"]
            in ("classification_progress", "classification_batch_started", "classification_plan")
        ),
        None,
    )
    if progress:
        completed = int(progress.get("completed", progress.get("reused", 0)))
        total = int(progress["total"])
        parts.append(
            '<section class="classification"><h2>Relevance classification</h2>'
            f"<p>{completed} / {total} jobs complete</p>"
            f'<progress aria-label="Jobs classified" value="{completed}" '
            f'max="{max(total, 1)}"></progress></section>'
        )
    if companies:
        location_counts = {e["company"]: e for e in events if e["stage"] == "location_filter"}
        parts.append(
            '<h2>Source results</h2><p class="hint">Location counts are before relevance '
            "classification. In-area jobs may still be unrelated to your search. "
            "Missing or unconfirmed locations are excluded from geographic scopes.</p>"
            '<div class="table-wrap"><table><thead><tr><th>Company</th><th>Result</th>'
            "<th>Fetched</th><th>In location scope</th><th>Filtered out</th>"
            "<th>New</th><th>Removed</th></tr></thead><tbody>"
        )
        # Put failures first so they are visible without scanning every successful source.
        for c in sorted(companies, key=lambda e: e["stage"] != "company_failed"):
            name = names.get(c["company"], c["company"])
            bad = c["stage"] == "company_failed"
            result = badge("Failed" if bad else "Completed", "error" if bad else "success")
            unknown = location_counts.get(c["company"], {}).get("unknown_kept", 0)
            note = f"<small>Unknown location kept: {int(unknown)}</small>" if unknown else ""
            parts.append(
                f'<tr><th scope="row">{escape(name)}'
                f"<small>{escape(c['company'])}</small>{note}</th>"
                f'<td data-label="Result">{result}</td>'
            )
            for key, title in [
                ("fetched", "Fetched"),
                ("kept", "In location scope"),
                ("dropped", "Filtered out"),
                ("new", "New"),
                ("removed", "Removed"),
            ]:
                parts.append(
                    f'<td data-label="{title}" class="number">{escape(str(c.get(key, "—")))}</td>'
                )
            parts.append("</tr>")
        parts.append(
            '</tbody></table></div><p class="hint">New means newly discovered, '
            "before relevance classification. Unknown location kept counts, when present, "
            "reflect the filtering rules used for that historical run.</p>"
        )
    if r["id"] != selected:
        parts.append(f'<a class="button" href="/logs?id={rid}">Open event timeline →</a>')
    else:
        parts.append(
            '<section class="timeline"><h2>Event timeline</h2>'
            '<p class="hint">Blue: activity · Green: completed · Amber: warning or retry '
            "· Red: error. Expand an event for technical details.</p>"
            '<div class="filters"><label>Show<select id="event-level">'
            '<option value="milestones">Milestones &amp; issues</option>'
            '<option value="issues">Warnings &amp; errors</option>'
            '<option value="all">All events</option></select></label>'
            '<label>Find an event<input id="event-search" type="search" '
            'placeholder="Company, event, URL…"></label></div>'
            '<p id="event-count" class="hint" aria-live="polite"></p><ol class="events">'
        )
        for e in events:
            color = tone(e)
            stage = e["stage"].replace("_", " ").capitalize()
            company = names.get(e.get("company"), e.get("company", ""))
            technical = {k: v for k, v in e.items() if k not in ("time", "stage")}
            routine = e["stage"] in ("request", "response", "crawl_delay", "robots_allowed")
            detail = e.get("error") or e.get("warning") or e.get("reason") or e.get("url") or ""
            code = f" · HTTP {e['status_code']}" if "status_code" in e else ""
            event_label = {
                "error": "Error",
                "warning": "Warning",
                "success": "Done",
                "info": "Activity",
            }[color]
            parts.append(
                f'<li class="event {color}" data-level="{color}" '
                f'data-routine="{str(routine).lower()}"><details><summary>'
                f"<time>{escape(stamp(e['time'], short=True))}</time>"
                f'<span class="event-heading"><strong>{escape(stage + code)}</strong>'
                f"<small>{escape(company)}</small></span>"
                f"{badge(event_label, color)}"
                "</summary>"
            )
            if detail:
                parts.append(f'<p class="event-message">{escape(str(detail))}</p>')
            parts.append(
                f"<pre>{escape(json.dumps(technical, ensure_ascii=False, indent=2))}</pre>"
                "</details></li>"
            )
        parts.append("</ol></section>")
    return "".join(parts) + "</div></details>"


STYLE = """
:root{color-scheme:light dark;
--bg:#f4f6f9;
--surface:#fff;
--text:#192331;
--muted:#627083;

--line:#dfe5ed;
--info:#245bcc;
--success:#167345;
--warning:#946000;
--error:#bf3038}

@media(prefers-color-scheme:dark){:root{--bg:#0e1115;
--surface:#161b23;
--text:#e6ebf2;

--muted:#9ba9bc;
--line:#2b3441;
--info:#8bb1ff;
--success:#6ad59c;
--warning:#efc46a;
--error:#ff9399}
}

*{box-sizing:border-box}
body{margin:0;
background:var(--bg);
color:var(--text);

font:14px/1.55 system-ui,-apple-system,sans-serif}
main{max-width:1120px;
margin:auto;
padding:32px 24px 64px}

a{color:var(--info)}
h1{font-size:28px;
letter-spacing:-.04em;
margin:14px 0 4px}

h2{font-size:16px;
margin:24px 0 8px}
p{margin:8px 0 16px}
.hint,small{color:var(--muted)}

.page-top{display:flex;
justify-content:space-between;
align-items:center;
gap:16px}

.button{display:inline-flex;
align-items:center;
min-height:40px;
padding:8px 14px;
border:1px solid var(--line);

border-radius:8px;
background:var(--surface);
color:var(--text);
text-decoration:none;
font:inherit;
cursor:pointer}

.run{margin:16px 0;
border:1px solid var(--line);
border-radius:12px;
background:var(--surface);
overflow:hidden}

summary{cursor:pointer}
.run>summary{display:flex;
align-items:center;
gap:16px;
padding:18px 20px;
list-style:none}

.run>summary::before{content:'›';
font-size:24px;
color:var(--muted)}
.run[open]>summary::before{transform:rotate(90deg)}

.run[open]>summary{border-bottom:1px solid var(--line)}
summary::-webkit-details-marker{display:none}

.run-title{flex:1;
font-size:15px;
font-weight:650}
.run-title small{display:block;
font-size:12px;
font-weight:400;
margin-top:3px}

.run-body{padding:20px}
.badge{display:inline-block;
white-space:nowrap;
font-size:11px;
font-weight:650;

border-radius:6px;
padding:4px 8px;
background:color-mix(in srgb,currentColor 11%,transparent)}

.info{color:var(--info)}
.success{color:var(--success)}
.warning{color:var(--warning)}
.error{color:var(--error)}

.muted{color:var(--muted)}
.metrics{display:grid;
grid-template-columns:repeat(5,1fr);
gap:12px}

.metric{border:1px solid var(--line);
border-radius:9px;
padding:12px}
.metric strong{display:block;
font-size:23px;

font-variant-numeric:tabular-nums;
letter-spacing:-.03em}
.metric span{color:var(--muted);
font-size:12px}

.issues{border-left:3px solid var(--error);
padding:2px 16px 8px;
margin:20px 0;

background:color-mix(in srgb,var(--error) 5%,transparent);
border-radius:0 8px 8px 0}

.issues h2{color:var(--error);
margin-top:12px}
.issue{padding:8px 0}
.issue p{margin:3px 0;
font-size:13px;
overflow-wrap:anywhere}

.notice{padding:12px;
border:1px solid var(--line);
border-radius:8px;
color:var(--muted)}

.classification p{margin-bottom:6px}
progress{width:100%;
height:8px;
accent-color:var(--info)}

.table-wrap{overflow:auto;
border:1px solid var(--line);
border-radius:9px}
table{border-collapse:collapse;
width:100%;
font-size:12px}

th,td{text-align:left;
padding:12px;
border-bottom:1px solid var(--line)}
thead{background:var(--bg);
color:var(--muted)}

th small{display:block;
font-weight:400;
font-size:10px;
overflow-wrap:anywhere}
tbody tr:last-child>*{border-bottom:0}

tbody tr:hover{background:var(--bg)}
.number{text-align:right;
font-variant-numeric:tabular-nums}

.filters{display:flex;
gap:12px}
.filters label{display:grid;
gap:5px;
font-size:12px;
color:var(--muted)}

.filters label:last-child{flex:1}
input,select{font:inherit;
color:var(--text);
background:var(--bg);
border:1px solid var(--line);

border-radius:8px;
padding:10px;
min-width:0;
min-height:42px}
input{width:100%}

.events{list-style:none;
padding:0}
.event{border:1px solid var(--line);
border-left:3px solid currentColor;

border-radius:8px;
margin:8px 0}
.event summary{display:flex;
align-items:center;
gap:12px;
padding:12px;
list-style:none}

.event summary::after{content:'+';
color:var(--muted)}
.event details[open] summary::after{content:'−'}

.event time{color:var(--muted);
font-size:11px;
font-variant-numeric:tabular-nums}
.event-heading{flex:1;
min-width:0;

color:var(--text);
font-size:13px}
.event-heading small{display:block}
.event-message{color:var(--text);
padding:0 12px;
overflow-wrap:anywhere}

pre{margin:0;
padding:14px;
background:var(--bg);
color:var(--muted);
border-top:1px solid var(--line);

font:12px/1.6 ui-monospace,monospace;
white-space:pre-wrap;
overflow-wrap:anywhere}

[hidden]{display:none!important}
:focus-visible{outline:2px solid var(--info);
outline-offset:3px}

@media(max-width:640px){main{padding:20px 12px 40px}
.run>summary{padding:14px 12px;
gap:8px}

.run-body{padding:12px}
.metrics{grid-template-columns:repeat(2,1fr);
gap:8px}
.metric:first-child{grid-column:1/-1}

.filters{flex-direction:column}
input,select{font-size:16px}
.event summary{flex-wrap:wrap;
gap:8px}

.event time{width:100%}
.run-title{font-size:14px}
.run>summary>.badge{font-size:10px}

thead{display:none}
table,tbody,tr,th,td{display:block}
tbody tr{display:grid;
grid-template-columns:1fr 1fr;

padding:12px;
border-bottom:1px solid var(--line);
gap:8px}
tbody th{grid-column:1/-1}
th,td{padding:0;
border:0}

td[data-label]{display:flex;
align-items:center;
justify-content:space-between;
gap:8px}

td[data-label]::before{content:attr(data-label);
color:var(--muted);
font-size:11px}
.table-wrap{overflow:visible}
}

"""

SCRIPT = """
const level = document.getElementById('event-level');
const search = document.getElementById('event-search');
function filterEvents() {
  const query = search.value.trim().toLowerCase();
  const events = [...document.querySelectorAll('.event')];
  let shown = 0;
  for (const event of events) {
    const issue = ['error', 'warning'].includes(event.dataset.level);
    const matches = level.value === 'all' || (level.value === 'issues' ? issue :
      issue || event.dataset.routine !== 'true');
    event.hidden = !matches || !event.textContent.toLowerCase().includes(query);
    if (!event.hidden) shown++;
  }
  document.getElementById('event-count').textContent = shown ?
    `${shown} of ${events.length} events shown` : 'No events match these filters.';
}
if (level) {
  level.addEventListener('change', filterEvents);
  search.addEventListener('input', filterEvents);
  filterEvents();
}
"""
