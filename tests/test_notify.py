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
    assert notify.subject(d) == "Job Scraper: nothing to report"


def test_text_and_html_render_all_sections():
    removed = posting(
        key="acme:2", title="QA Tester", removed_at=datetime(2026, 10, 11, tzinfo=UTC)
    )
    failing = HealthEvent(
        "acme", "failing", Health(consecutive_failures=3, last_error="timeout", failing_since=T0)
    )
    d = notify.Digest(
        new=[posting(posted_date=date(2026, 9, 30))], removed=[removed], health=[failing]
    )

    assert notify.subject(d) == "Job Scraper: 1 new, 1 removed, site check failing"
    text = notify.render_text(d, NAMES)
    assert "Acme Corp" in text
    assert "[entry, posted 2026-09-30]" in text
    assert "QA Tester at Acme Corp, up 10 days" in text
    assert "3 failed checks in a row since 2026-10-01 00:00 UTC (timeout)" in text

    html = notify.render_html(d, NAMES)
    assert "Dev &lt;I&gt;" in html
    assert 'href="https://x/1?a=1&amp;b=2"' in html


def test_reasons_only_when_asked():
    d = notify.Digest(new=[posting(classification={"level": "entry", "reason": "dev role"})])
    assert "why: dev role" in notify.render_text(d, NAMES, show_reasons=True)
    assert "why:" not in notify.render_text(d, NAMES)


def test_first_seen_used_without_posted_date():
    text = notify.render_text(notify.Digest(new=[posting()]), NAMES)
    assert "first seen 2026-10-01" in text


def test_build_message_has_both_parts():
    msg = notify.build_message(notify.Digest(new=[posting()]), NAMES, "to@x.com", "from@x.com")
    assert msg["To"] == "to@x.com"
    assert [p.get_content_type() for p in msg.iter_parts()] == ["text/plain", "text/html"]
