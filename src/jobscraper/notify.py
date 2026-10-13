import os
import smtplib
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from html import escape

from jobscraper.state import Posting
from jobscraper.tracker import HealthEvent

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


@dataclass
class Digest:
    new_exact: list[Posting] = field(default_factory=list)
    new_near: list[Posting] = field(default_factory=list)
    new_maybe: list[Posting] = field(default_factory=list)
    removed: list[Posting] = field(default_factory=list)
    health: list[HealthEvent] = field(default_factory=list)
    # Reminder only; doesn't trigger an email by itself.
    still_available: list[Posting] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.new_exact or self.new_near or self.new_maybe or self.removed or self.health
        )

    def job_sections(self) -> list[tuple[str, list[Posting]]]:
        sections = [
            ("New exact matches", self.new_exact),
            ("New near matches", self.new_near),
            ("New probably-not matches (Claude unsure)", self.new_maybe),
            ("Still available exact matches", self.still_available),
        ]
        return [(title, postings) for title, postings in sections if postings]


def _posted(p: Posting) -> str:
    if p.posted_date:
        return f"posted {p.posted_date.isoformat()}"
    return f"first seen {p.first_seen.date().isoformat()}"


def _level(p: Posting) -> str:
    return (p.classification or {}).get("level", "unknown")


def _days_up(p: Posting) -> int:
    end = p.removed_at or p.last_seen
    return (end - p.first_seen).days


def _source(p: Posting, names: dict[str, str]) -> str:
    name = names.get(p.company_id, p.company_id)
    return f"{p.employer} (via {name})" if p.employer else name


def _by_company(postings: list[Posting], names: dict[str, str]) -> dict[str, list[Posting]]:
    groups: dict[str, list[Posting]] = {}
    for p in sorted(postings, key=lambda p: (_source(p, names).lower(), p.title)):
        groups.setdefault(_source(p, names), []).append(p)
    return groups


def _health_line(e: HealthEvent, names: dict[str, str]) -> str:
    name = names.get(e.company_id, e.company_id)
    if e.kind == "recovered":
        return f"{name}: checks are working again"
    since = e.health.failing_since.strftime("%Y-%m-%d %H:%M UTC") if e.health.failing_since else "?"
    return (
        f"{name}: {e.health.consecutive_failures} failed checks in a row since {since} "
        f"({e.health.last_error})"
    )


def subject(d: Digest) -> str:
    parts = [
        f"{len(postings)} {label}"
        for label, postings in (
            ("exact", d.new_exact),
            ("near", d.new_near),
            ("maybe", d.new_maybe),
            ("removed", d.removed),
        )
        if postings
    ]
    if any(e.kind == "failing" for e in d.health):
        parts.append("site check failing")
    return "Job Scraper: " + (", ".join(parts) or "nothing to report")


def render_text(d: Digest, names: dict[str, str], show_reasons: bool = False) -> str:
    out = []
    for title, postings in d.job_sections():
        out.append(title.upper())
        for source, group in _by_company(postings, names).items():
            out.append(f"\n{source}")
            for p in group:
                out.append(f"  - {p.title} ({p.location}) [{_level(p)}, {_posted(p)}]")
                if show_reasons:
                    out.append(f"    why: {(p.classification or {}).get('reason', '-')}")
                out.append(f"    {p.url}")
        out.append("")
    if d.removed:
        out.append("REMOVED JOBS")
        for p in sorted(d.removed, key=lambda p: (p.company_id, p.title)):
            out.append(f"  - {p.title} at {_source(p, names)}, up {_days_up(p)} days")
        out.append("")
    if d.health:
        out.append("SITE HEALTH")
        out.extend(f"  - {_health_line(e, names)}" for e in d.health)
        out.append("")
    return "\n".join(out) or "Nothing to report."


def render_html(d: Digest, names: dict[str, str]) -> str:
    out = []
    for title, postings in d.job_sections():
        out.append(f"<h2>{escape(title)}</h2>")
        for source, group in _by_company(postings, names).items():
            out.append(f"<h3>{escape(source)}</h3><ul>")
            for p in group:
                out.append(
                    f'<li><a href="{escape(p.url)}">{escape(p.title)}</a> '
                    f"&middot; {escape(p.location)} &middot; {escape(_level(p))} "
                    f"&middot; {escape(_posted(p))}</li>"
                )
            out.append("</ul>")
    if d.removed:
        out.append("<h2>Removed jobs</h2><ul>")
        for p in sorted(d.removed, key=lambda p: (p.company_id, p.title)):
            out.append(
                f"<li>{escape(p.title)} at {escape(_source(p, names))}, up {_days_up(p)} days</li>"
            )
        out.append("</ul>")
    if d.health:
        out.append("<h2>Site health</h2><ul>")
        out.extend(f"<li>{escape(_health_line(e, names))}</li>" for e in d.health)
        out.append("</ul>")
    return "<html><body>" + "".join(out) + "</body></html>"


def build_message(d: Digest, names: dict[str, str], to: str, sender: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject(d)
    msg["From"] = sender
    msg["To"] = to
    msg["Date"] = datetime.now().astimezone().strftime("%a, %d %b %Y %H:%M:%S %z")
    msg.set_content(render_text(d, names))
    msg.add_alternative(render_html(d, names), subtype="html")
    return msg


def send(msg: EmailMessage) -> None:
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_APP_PASSWORD"]
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)
