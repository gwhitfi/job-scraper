# ruff: noqa: E501
"""A focused, auditable worklist for first-job software opportunities."""

import math
import re
from collections import Counter
from html import escape
from pathlib import Path
from urllib.parse import urlencode

from jobscraper.classification import jev_triage
from jobscraper.classification import jev_v6 as screening
from jobscraper.storage import userdata
from jobscraper.workflow.notify import safe_url

VIEWS = {
    "matches": "Matches",
    "exact": "Strong fits",
    "evaluate": "Needs review",
    "starred": "Shortlist",
    "applied": "Applied",
    "pending": "Pending assessment",
    "rejected": "Not a fit",
    "unverified": "Verify employer",
    "blocked": "Blocked employers",
    "hidden": "Hidden",
}
LABELS = {
    "exact": "Strong fit",
    "evaluate": "Needs review",
    "pending": "Pending assessment",
    "rejected": "Not a fit",
}
SIGNALS = {
    "eligibility": {"clear": "No extra restriction found", "verify": "Eligibility to confirm"},
    "programming": {
        "primary": "Coding is central",
        "regular": "Regular coding",
        "incidental": "Limited coding",
        "none": "Not programming",
        "unclear": "Duties unclear",
    },
    "level": {
        "entry": "Junior / 0–2 years",
        "mid": "Experience stretch",
        "senior": "Senior level",
        "internship": "Student / internship",
        "unsure": "Experience unclear",
    },
    "degree": {
        "cs_listed": "CS accepted",
        "generic_or_none": "Degree compatible",
        "preferred_only": "Degree only preferred",
        "excluded": "Required degree mismatch",
        "unclear": "Education unclear",
    },
}
PAGE_SIZE = 20


def link(view: str, query: dict, **changes) -> str:
    values = {k: v for k, v in query.items() if k in ("q", "sort", "source", "area") and v}
    values.update(jev="v6", view=view)
    values.update(changes)
    return "/jev?" + urlencode(values)


def bucket(row) -> str:
    for mark in ("hidden", "applied", "starred"):
        if row.marks.get(mark):
            return mark
    if row.employer_status in ("blocked", "unverified"):
        return row.employer_status
    return screening.decision(row.jev).group


def excerpts(record: dict | None) -> tuple[str, list[str]]:
    job = (record or {}).get("request", {}).get("state", {}).get("job", {})
    full = job.get("description_complete") and job.get("description")
    text = job.get("description") if full else job.get("summary", "")
    parts = re.split(r"\n+|(?<=[.!?])\s+", text or "")
    selected = [
        p.strip()
        for p in parts
        if re.search(
            r"program|develop|software|degree|bachelor|computer science|years?|experience|qualif|requir",
            p,
            re.I,
        )
    ]
    return ("Full-description excerpts" if full else "Summary excerpts"), selected[:5]


def card(row, eligible: bool = True) -> str:
    p = row.posting
    decision = screening.decision(row.jev)
    answers = row.jev["response"]["answers"] if row.jev else {}
    badges = []
    if not eligible and p.status == "active":
        badges.append('<span class="flag">Outside current scope or source paused</span>')
    if p.status != "active":
        badges.append('<span class="flag">Closed listing</span>')
    if p.area == "extended":
        badges.append('<span class="flag">Extended area</span>')
    if row.employer_status in ("unverified", "blocked"):
        badges.append(f'<span class="flag">Employer {escape(row.employer_status)}</span>')
    signals = "".join(
        f"<div><dt>{escape(name.title())}</dt><dd>{escape(labels[a['choice']])}"
        f"{' · uncertain' if a['confidence'] < 0.6 else ''}</dd></div>"
        for name, labels in SIGNALS.items()
        if (a := answers.get(name))
    )
    reason_list = "".join(f"<li>{escape(reason)}</li>" for reason in decision.reasons)
    origin, quotes = excerpts(row.jev or {"request": {"state": {"job": {"summary": p.summary}}}})
    evidence = "".join(f"<blockquote>{escape(q)}</blockquote>" for q in quotes)
    job = (row.jev or {}).get("request", {}).get("state", {}).get("job", {})
    full = job.get("description", "") if job.get("description_complete") else ""
    detail = (
        '<details class="full-text"><summary>Read saved description</summary>'
        f'<div class="description">{escape(full)}</div></details>'
        if full
        else '<p class="muted">Full description unavailable. Check the original listing before deciding.</p>'
    )
    buttons = "".join(
        f'<button type="button" class="mark {"selected" if row.marks.get(field) else ""}" '
        f'data-field="{field}" aria-pressed="{str(bool(row.marks.get(field))).lower()}">'
        f"{on if row.marks.get(field) else off}</button>"
        for field, on, off in (
            ("starred", "★ Shortlisted", "☆ Shortlist"),
            ("applied", "✓ Applied", "Mark applied"),
            ("hidden", "Unhide", "Hide"),
        )
    )
    source_link = escape(safe_url(p.url), quote=True)
    distance = f" · {round(row.miles)} miles" if row.miles is not None else ""
    confidence = "".join(
        f"<li>{escape(name.title())}: {escape(a['choice'].replace('_', ' '))} "
        f"({a['confidence']:.0%} model confidence)</li>"
        for name, a in answers.items()
    )
    employer_controls = ""
    if row.is_board:
        employer_controls = (
            '<details class="employer"><summary>Employer decision</summary>'
            "<p>Approve an employer you trust, or block its listings across the site.</p>"
            + "".join(
                f'<button type="button" class="employer-action" data-action="{action}" '
                f'data-employer="{escape(row.employer_key, quote=True)}">{label}</button>'
                for action, label in (
                    ("approve", "Approve employer"),
                    ("block", "Block employer"),
                    ("clear", "Clear decision"),
                )
            )
            + "</details>"
        )
    return f'''<article class="job" data-key="{escape(p.key, quote=True)}">
<div class="job-top"><span class="verdict {decision.group}">{LABELS[decision.group]}</span>
<span class="muted">Seen {p.first_seen:%b %d}</span></div>
<h2><a href="{source_link}" target="_blank" rel="noopener noreferrer">{escape(p.title)}</a></h2>
<p class="company">{escape(row.source)}</p>
<p class="location">{escape(p.location or "Location not confirmed")}{distance}</p>
<div class="flags">{"".join(badges)}</div>
<dl class="signals">{signals}</dl><ul class="reasons">{reason_list}</ul>
<details class="evidence"><summary>Requirements and assessment</summary>
<h3>{origin}</h3>{evidence or "<p>No source excerpts are available yet.</p>"}
<p class="muted">Excerpts are source text, not proof of eligibility. Check the complete requirements.</p>
{detail}<details><summary>Assessment details</summary><ul>{confidence}</ul>
<p class="muted">Model confidence is uncertainty about an answer, not your chance of being hired.
Full text means the parser accepted the description; omissions are still possible.</p></details></details>
<div class="job-bottom"><a class="open-job" href="{source_link}" target="_blank"
rel="noopener noreferrer">View original job ↗</a><div class="actions">{buttons}</div></div>
<details class="notes"><summary>Notes</summary><label>Notes for {escape(p.title)}
<textarea maxlength="{userdata.MAX_NOTE}" rows="3">{escape(row.marks.get("note", ""))}</textarea>
</label><button type="button" class="save-note">Save note</button></details>
{employer_controls}<p class="save-status" role="status"></p></article>'''


def page(app, query: dict[str, str]) -> str:
    view = query.get("view", "matches")
    if view not in VIEWS:
        view = "matches"
    q = query.get("q", "").strip()
    source = query.get("source", "all")
    area = query.get("area", "all")
    sort = query.get("sort", "fit")
    rows = app.rows("jev", "v6")
    enabled = {c.id for c in app.cfg.companies if c.enabled}
    eligible = [
        r
        for r in rows
        if r.posting.company_id in enabled and jev_triage.in_scope(r.posting, app.scopes)
    ]
    coverage = Counter(screening.decision(r.jev).group for r in eligible)
    total = len(eligible)
    assessed = total - coverage["pending"]
    personal = {
        r.posting.key for r in rows if any(r.marks.get(k) for k in ("starred", "applied", "hidden"))
    }
    eligible_keys = {r.posting.key for r in eligible}
    candidates = [r for r in rows if r.posting.key in eligible_keys | personal]
    candidates = [
        r
        for r in candidates
        if app.matches_query(r, q)
        and (source not in ("companies", "boards") or app.in_source(r, source))
        and (area not in ("primary", "extended") or r.posting.area == area)
    ]
    counts = Counter(bucket(r) for r in candidates)
    counts["matches"] = counts["exact"] + counts["evaluate"]
    shown = [
        r
        for r in candidates
        if bucket(r) == view or (view == "matches" and bucket(r) in ("exact", "evaluate"))
    ]
    if sort == "newest":
        shown.sort(key=lambda r: -r.posting.first_seen.timestamp())
    elif sort == "nearest":
        shown.sort(key=lambda r: (r.miles is None, r.miles or 0))
    else:
        shown.sort(
            key=lambda r: (
                screening.decision(r.jev).group != "exact",
                screening.rank(r.jev),
                -r.posting.first_seen.timestamp(),
            )
        )
    pages = max(1, math.ceil(len(shown) / PAGE_SIZE))
    try:
        number = max(1, min(int(query.get("p", "1")), pages))
    except ValueError:
        number = 1
    cards = "".join(
        card(r, eligible=r.posting.key in eligible_keys)
        for r in shown[(number - 1) * PAGE_SIZE : number * PAGE_SIZE]
    )
    if not cards:
        message = (
            "Assessment is still running. Open Pending assessment to inspect jobs."
            if coverage["pending"] and view in ("matches", "exact", "evaluate")
            else "No jobs in this view. Try clearing the search or checking another section."
        )
        cards = (
            f'<div class="empty"><h2>No jobs found</h2><p>{message}</p>'
            + f'<a href="{escape(link("pending", {}))}">Open pending assessment →</a></div>'
        )
    nav = "".join(
        f'<a class="nav-item {"active" if key == view else ""}" '
        f'{"aria-current=page" if key == view else ""} href="{escape(link(key, query))}">'
        f"<span>{label}</span><b>{counts[key]}</b></a>"
        for key, label in VIEWS.items()
    )

    def options(values, selected):
        return "".join(
            f'<option value="{value}" {"selected" if value == selected else ""}>{label}</option>'
            for value, label in values
        )

    pager = "".join(
        f'<a href="{escape(link(view, query, p=n))}">{label}</a>'
        for n, label in ((number - 1, "← Previous"), (number + 1, "Next →"))
        if 1 <= n <= pages
    )
    pct = round(100 * assessed / total) if total else 0
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Jev v6 · Jobs</title>
<style>{STYLE}</style></head><body><a class="skip" href="#results">Skip to jobs</a>
<header class="masthead"><a class="brand" href="/jev?jev=v6">Jev<span class="edition">v6</span></a>
<nav aria-label="Main"><a href="/">All jobs</a><a href="/companies">Companies</a><a href="/logs">Crawl logs</a></nav>
</header>
<div class="layout"><aside><p class="eyebrow">JOB VIEWS</p><nav aria-label="Job views">{nav}</nav>
<div class="profile"><details open><summary>Matching criteria</summary>
<ul><li>0–2 required years; modest stretches go to review.</li><li>Generic degrees accepted. Preferred fields are flexible.</li>
<li>No industrial controls, senior/management, or student-only internships.</li>
<li>Unclear requirements stay visible for review.</li><li>Extended-area review candidates are included and labeled.</li></ul></details></div>
</aside>
<main id="results"><section class="intro"><div><h1>Job matches</h1>
<p class="lede">Entry-level software roles and jobs needing review.</p></div></section>
<section class="overview" aria-label="Assessment overview">
<a href="{escape(link("exact", {}))}"><span>STRONG FITS</span><strong>{coverage["exact"]}</strong><small>Clear junior programming signals</small></a>
<a href="{escape(link("evaluate", {}))}"><span>NEEDS REVIEW</span><strong>{coverage["evaluate"]}</strong><small>Uncertain requirements or experience</small></a>
<a href="{escape(link("pending", {}))}"><span>ASSESSED</span><strong>{assessed}<i> / {total}</i></strong><small>{coverage["pending"]} awaiting current v6 answers</small><progress value="{pct}" max="100">{pct}%</progress></a></section>
<p class="coverage-note">Overview includes all active jobs in your area. Shortlisted, applied, hidden,
or employer-restricted jobs have their own sections. Counts are not accuracy estimates.</p>
<div class="results-heading"><div><p class="eyebrow">{counts[view]} JOBS</p><h2>{VIEWS[view]}</h2></div>
<a class="refresh" href="{escape(link(view, query, p=number))}">Refresh results ↻</a></div>
<form class="filters" method="get"><input type="hidden" name="jev" value="v6"><input type="hidden" name="view" value="{view}">
<label class="search">Search jobs<input name="q" value="{escape(q, quote=True)}" placeholder="Role, company or location…"></label>
<label>Source<select name="source">{options([("all", "All sources"), ("companies", "Company sites"), ("boards", "Job boards")], source)}</select></label>
<label>Area<select name="area">{options([("all", "All watched areas"), ("primary", "Primary area"), ("extended", "Extended area")], area)}</select></label>
<label>Order<select name="sort">{options([("fit", "Best fit first"), ("newest", "Newest first"), ("nearest", "Nearest first")], sort)}</select></label>
<button type="submit">Apply filters</button><a href="{escape(link(view, {}))}">Reset</a></form>
<section class="job-list" aria-label="Job results">{cards}</section>
<nav class="pager" aria-label="Results pages"><span>Page {number} of {pages}</span>{pager}</nav>
<footer>Jev v6 · Verify requirements in the original listing.</footer>
</main></div><script>{SCRIPT}</script></body></html>'''


STYLE = Path(__file__).with_suffix(".css").read_text(encoding="utf-8")
SCRIPT = Path(__file__).with_suffix(".js").read_text(encoding="utf-8")
