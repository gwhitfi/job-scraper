from datetime import UTC, date, datetime

from jobscraper import notify
from jobscraper.state import Health, Posting
from jobscraper.tracker import HealthEvent

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
    assert d.is_empty()
    assert notify.Digest(still_available=[posting()]).is_empty()  # reminder alone isn't news
    assert notify.subject(d) == "Job Scraper: nothing to report"


def test_text_and_html_render_all_sections():
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

    html = notify.render_html(d, NAMES)
    assert "<h2>Still available exact matches</h2>" in html
    assert "Dev &lt;I&gt;" in html
    assert 'href="https://x/1?a=1&amp;b=2"' in html


def test_reasons_only_when_asked():
    d = notify.Digest(new_exact=[posting(classification={"level": "entry", "reason": "dev role"})])
    assert "why: dev role" in notify.render_text(d, NAMES, show_reasons=True)
    assert "why:" not in notify.render_text(d, NAMES)


def test_first_seen_used_without_posted_date():
    text = notify.render_text(notify.Digest(new_exact=[posting()]), NAMES)
    assert "first seen 2026-10-01" in text


def test_build_message_has_both_parts():
    msg = notify.build_message(
        notify.Digest(new_exact=[posting()]), NAMES, "to@x.com", "from@x.com"
    )
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
    msg = notify.build_message(d, NAMES, "to@x.com", "from@x.com", "https://jobs.example.ts.net")
    assert msg["Subject"] == "Job Scraper: 1 new exact match"
    text = msg.get_body(("plain",)).get_content()
    assert "Web Developer" in text
    assert "Data Analyst" not in text and "Old Dev" not in text
    assert "All jobs: https://jobs.example.ts.net" in text
    html = msg.get_body(("html",)).get_content()
    assert 'href="https://jobs.example.ts.net"' in html
