import logging
import os
import smtplib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from html import escape
from pathlib import Path

from jobscraper import jsonfile
from jobscraper.state import Posting
from jobscraper.tracker import HealthEvent

log = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
MAX_PENDING = 20  # unsent alerts kept for retry; older ones are dropped


@dataclass
class Digest:
    new_exact: list[Posting] = field(default_factory=list)
    new_near: list[Posting] = field(default_factory=list)
    new_maybe: list[Posting] = field(default_factory=list)
    removed: list[Posting] = field(default_factory=list)
    health: list[HealthEvent] = field(default_factory=list)
    # Reminder only; doesn't trigger an email by itself.
    still_available: list[Posting] = field(default_factory=list)
    unverified: set[str] = field(default_factory=set)  # keys from employers I haven't vetted

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
    return p.source_label(names.get(p.company_id, p.company_id))


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
                flag = " (unverified employer)" if p.key in d.unverified else ""
                out.append(f"  - {p.title} ({p.location}) [{_level(p)}, {_posted(p)}]{flag}")
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
                    f"&middot; {escape(_posted(p))}"
                    + (" &middot; <b>unverified employer</b>" if p.key in d.unverified else "")
                    + "</li>"
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


def needs_alert(d: Digest) -> bool:
    """The email only covers new exact matches and site health; the web page has the rest."""
    return bool(d.new_exact or d.health)


def alert_subject(d: Digest) -> str:
    parts = []
    if d.new_exact:
        n = len(d.new_exact)
        parts.append(f"{n} new exact match{'es' if n != 1 else ''}")
    if any(e.kind == "failing" for e in d.health):
        parts.append("site check failing")
    elif d.health:
        parts.append("site check recovered")
    return "Job Scraper: " + ", ".join(parts)


def render_alert_text(d: Digest, names: dict[str, str], page_url: str) -> str:
    alert = Digest(new_exact=d.new_exact, health=d.health, unverified=d.unverified)
    text = render_text(alert, names)
    return f"{text}\nAll jobs: {page_url}\n" if page_url else text


def render_alert_html(d: Digest, names: dict[str, str], page_url: str) -> str:
    alert = Digest(new_exact=d.new_exact, health=d.health, unverified=d.unverified)
    html = render_html(alert, names)
    if page_url:
        link = f'<p><a href="{escape(page_url)}">Open all jobs</a></p>'
        html = html.replace("</body>", link + "</body>")
    return html


def compose(d: Digest, names: dict[str, str], page_url: str = "") -> dict:
    """The alert's contents, in a form that can wait in the outbox."""
    return {
        "subject": alert_subject(d),
        "text": render_alert_text(d, names, page_url),
        "html": render_alert_html(d, names, page_url),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def to_message(alert: dict, to: str, sender: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = alert["subject"]
    msg["From"] = sender
    msg["To"] = to
    msg["Date"] = datetime.now().astimezone().strftime("%a, %d %b %Y %H:%M:%S %z")
    msg.set_content(alert["text"])
    msg.add_alternative(alert["html"], subtype="html")
    return msg


def build_message(
    d: Digest, names: dict[str, str], to: str, sender: str, page_url: str = ""
) -> EmailMessage:
    return to_message(compose(d, names, page_url), to, sender)


# --- outbox ---
# Alerts wait in <state dir>/email.json until sent, so a mail problem (e.g. an expired app
# password) is retried next run instead of blocking state from being saved.


def load_outbox(path: Path) -> dict:
    return {"pending": [], "last_sent_at": None, "last_error": None, "last_error_at": None} | (
        jsonfile.read(path)
    )


def save_outbox(path: Path, outbox: dict) -> None:
    jsonfile.write(path, outbox)


def queue(outbox: dict, alert: dict) -> None:
    outbox["pending"] = (outbox["pending"] + [alert])[-MAX_PENDING:]


def flush(outbox: dict, to: str, sender: str, send_fn=None) -> int:
    """Send pending alerts oldest first; stop at the first failure. Returns how many were sent."""
    send_fn = send_fn or send
    sent = 0
    while outbox["pending"]:
        try:
            send_fn(to_message(outbox["pending"][0], to, sender))
        except Exception as e:
            outbox["last_error"] = f"{type(e).__name__}: {e}"
            outbox["last_error_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            log.error("Email failed, will retry next run: %s", outbox["last_error"])
            break
        outbox["pending"].pop(0)
        outbox["last_sent_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        outbox["last_error"] = outbox["last_error_at"] = None
        sent += 1
    return sent


def send(msg: EmailMessage) -> None:
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_APP_PASSWORD"]
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)
