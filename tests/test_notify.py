from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from jobscraper.storage import jsonfile
from jobscraper.storage.state import Health, Posting
from jobscraper.workflow import notify
from jobscraper.workflow.tracker import HealthEvent

T0 = datetime(2026, 10, 1, tzinfo=UTC)
NAMES = {"acme": "Acme Corp"}


def posting(**kw) -> Posting:
    base = dict(
        key="acme:1",
        company_id="acme",
        source_id="1",
        title="Dev <I>",
        location="Greenville, SC",
        url="https://x/1?a=1&b=2",
        posted_date=None,
        first_seen=T0,
        last_seen=T0,
        classification={"relevance": "core", "level": "entry"},
    )
    return Posting(**{**base, **kw})


def test_empty_digest():
    d = notify.Digest()
    assert not notify.needs_alert(d)
    assert not notify.needs_alert(notify.Digest(still_available=[posting()]))
    assert notify.subject(d) == "Job Scraper: nothing to report"


def test_text_renders_all_sections():
    removed = posting(
        key="acme:2", title="QA Tester", removed_at=datetime(2026, 10, 11, tzinfo=UTC)
    )
    failing = HealthEvent(
        "acme", "failing", Health(consecutive_failures=3, last_error="timeout", failing_since=T0)
    )
    d = notify.Digest(
        new_exact=[posting(posted_date=date(2026, 9, 30))],
        new_near=[posting(key="acme:3", title="Data Analyst")],
        new_maybe=[posting(key="acme:4", title="Systems Specialist")],
        still_available=[posting(key="acme:5", title="Web Developer")],
        removed=[removed],
        health=[failing],
    )

    assert notify.subject(d) == (
        "Job Scraper: 1 exact, 1 near, 1 maybe, 1 removed, site check failing"
    )
    text = notify.render_text(d, NAMES)
    headings = [
        "NEW EXACT MATCHES",
        "NEW NEAR MATCHES",
        "NEW PROBABLY-NOT MATCHES",
        "STILL AVAILABLE EXACT MATCHES",
        "REMOVED JOBS",
        "SITE HEALTH",
    ]
    positions = [text.index(h) for h in headings]
    assert positions == sorted(positions)
    assert text.index("Data Analyst") > text.index("NEW NEAR MATCHES")
    assert text.index("Web Developer") > text.index("STILL AVAILABLE EXACT MATCHES")
    assert "Acme Corp" in text
    assert "[entry, posted 2026-09-30]" in text
    assert "QA Tester at Acme Corp, up 10 days" in text
    assert "3 failed checks in a row since 2026-10-01 00:00 UTC (timeout)" in text

    html = notify.render_alert_html(d, NAMES, "")
    assert "Still available" not in html
    assert "Data Analyst" not in html and "QA Tester" not in html
    assert "<h2>Site health</h2>" in html
    assert "Dev &lt;I&gt;" in html
    assert 'href="https://x/1?a=1&amp;b=2"' in html


def test_reasons_only_when_asked():
    d = notify.Digest(new_exact=[posting(classification={"level": "entry", "reason": "dev role"})])
    assert "why: dev role" in notify.render_text(d, NAMES, show_reasons=True)
    assert "why:" not in notify.render_text(d, NAMES)


def test_first_seen_used_without_posted_date():
    text = notify.render_text(notify.Digest(new_exact=[posting()]), NAMES)
    assert "first seen 2026-10-01" in text


def test_outbox_message_has_both_parts():
    alert = notify.compose(notify.Digest(new_exact=[posting()]), NAMES)
    msg = notify.to_message(alert, "to@x.com", "from@x.com")
    assert msg["To"] == "to@x.com"
    assert [p.get_content_type() for p in msg.iter_parts()] == ["text/plain", "text/html"]


def test_board_postings_grouped_by_employer():
    names = {"mus": "Move Upstate SC"}
    d = notify.Digest(
        new_exact=[
            posting(key="mus:1", company_id="mus", title="BI Developer", employer="AFL"),
            posting(key="mus:2", company_id="mus", title="Data Engineer", employer="AFL"),
            posting(key="mus:3", company_id="mus", title="Web Dev", employer=""),
        ]
    )
    text = notify.render_text(d, names)
    assert text.count("AFL (via Move Upstate SC)") == 1
    assert "\nMove Upstate SC\n" in text  # card without an employer


def test_alert_only_for_new_exact_or_health():
    assert not notify.needs_alert(notify.Digest(new_near=[posting()], removed=[posting()]))
    assert notify.needs_alert(notify.Digest(new_exact=[posting()]))
    failing = HealthEvent("acme", "failing", Health(consecutive_failures=3))
    assert notify.needs_alert(notify.Digest(health=[failing]))


def test_alert_email_has_exact_matches_and_link_only():
    d = notify.Digest(
        new_exact=[posting(title="Web Developer")],
        new_near=[posting(key="acme:2", title="Data Analyst")],
        still_available=[posting(key="acme:3", title="Old Dev")],
    )
    msg = notify.to_message(
        notify.compose(d, NAMES, "https://jobs.example.ts.net"), "to@x.com", "from@x.com"
    )
    assert msg["Subject"] == "Job Scraper: 1 new exact match"
    text = msg.get_body(("plain",)).get_content()
    assert "Web Developer" in text
    assert "Data Analyst" not in text and "Old Dev" not in text
    assert "All jobs: https://jobs.example.ts.net" in text
    html = msg.get_body(("html",)).get_content()
    assert 'href="https://jobs.example.ts.net"' in html


def test_alert_labels_unverified_employers():
    p = posting(key="mus:1", company_id="mus", title="Remote Dev", employer="Shady LLC")
    d = notify.Digest(new_exact=[p], unverified={"mus:1"})
    msg = notify.to_message(notify.compose(d, {"mus": "Board"}), "to@x.com", "from@x.com")
    assert "Remote Dev" in msg.get_body(("plain",)).get_content()
    assert "(unverified employer)" in msg.get_body(("plain",)).get_content()
    assert "unverified employer" in msg.get_body(("html",)).get_content()


def test_outbox_sends_and_clears(tmp_path):
    path = tmp_path / "email.json"
    outbox = notify.load_outbox(path)
    notify.queue(outbox, notify.compose(notify.Digest(new_exact=[posting()]), NAMES))
    sent = []
    assert notify.flush(outbox, "to@x.com", "from@x.com", sent.append) == 1
    assert outbox["pending"] == [] and outbox["last_error"] is None
    assert sent[0]["Subject"] == "Job Scraper: 1 new exact match"
    jsonfile.write(path, outbox)
    assert notify.load_outbox(path)["last_sent_at"]


def test_outbox_keeps_alert_when_sending_fails(tmp_path):
    outbox = notify.load_outbox(tmp_path / "email.json")
    notify.queue(outbox, notify.compose(notify.Digest(new_exact=[posting()]), NAMES))

    def fail(msg):
        raise OSError("535 bad credentials")

    assert notify.flush(outbox, "to@x.com", "from@x.com", fail) == 0
    assert len(outbox["pending"]) == 1
    assert "535 bad credentials" in outbox["last_error"]

    # Next run: a new alert queues behind the old one and both go out once mail works.
    notify.queue(outbox, notify.compose(notify.Digest(new_exact=[posting()]), NAMES))
    sent = []
    assert notify.flush(outbox, "to@x.com", "from@x.com", sent.append) == 2
    assert outbox["last_error"] is None


def test_outbox_caps_pending():
    outbox = notify.load_outbox(Path("/nonexistent/email.json"))
    for i in range(notify.MAX_PENDING + 5):
        notify.queue(outbox, {"subject": str(i), "text": "", "html": ""})
    assert len(outbox["pending"]) == notify.MAX_PENDING
    assert outbox["pending"][0]["subject"] == "5"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://jobs.example.com/1", "https://jobs.example.com/1"),
        ("HTTP://example.com", "HTTP://example.com"),
        ("javascript:alert(1)", "#"),
        ("  javascript:alert(1)", "#"),
        ("data:text/html,<script>", "#"),
        ("/relative/path", "#"),
    ],
)
def test_safe_url(url, expected):
    assert notify.safe_url(url) == expected


def test_email_drops_unsafe_links():
    d = notify.Digest(new_exact=[posting(url="javascript:alert(1)")])
    assert 'href="#"' in notify.render_alert_html(d, NAMES, "")
