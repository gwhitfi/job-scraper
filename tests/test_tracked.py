from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from jobscraper.config import Config, Notifications, Profile
from jobscraper.models import Company
from jobscraper.presentation import tracked
from jobscraper.storage.state import Health, State

NOW = datetime(2026, 10, 14, 23, tzinfo=UTC)
COMPANY = Company("acme", "Acme", "generic", "https://acme.example/jobs", date(2026, 1, 1))


@pytest.mark.parametrize(
    ("health", "kind", "label"),
    [
        (None, "muted", "Waiting for first crawl"),
        (Health(last_success=NOW, last_count=0), "good", "Good"),
        (
            Health(last_success=NOW, last_warning="Recovered after timeout"),
            "warning",
            "Crawling with errors",
        ),
        (Health(last_success=NOW, consecutive_failures=1), "warning", "Crawling with errors"),
        (Health(last_success=NOW, consecutive_failures=2), "warning", "Crawling with errors"),
        (Health(last_success=NOW, consecutive_failures=3), "bad", "Failing to crawl"),
        (Health(consecutive_failures=1), "bad", "Failing to crawl"),
        (Health(last_success=NOW - timedelta(hours=7)), "warning", "Check overdue"),
    ],
)
def test_company_status(health, kind, label):
    assert tracked.status(COMPANY, health, NOW, 6)[:2] == (kind, label)


def test_paused_is_not_reported_as_failing():
    assert tracked.status(replace(COMPANY, enabled=False), Health(consecutive_failures=10), NOW, 6)[
        :2
    ] == ("muted", "Paused")


def test_page_escapes_errors_names_and_links():
    company = replace(COMPANY, name="<script>oops()</script>", url="javascript:alert(1)")
    cfg = Config(Profile("dev"), Notifications("test@example.com"), [company])
    html = tracked.page(
        cfg,
        State(
            health={
                "acme": Health(consecutive_failures=1, last_error="<img src=x onerror=alert(1)>")
            }
        ),
        stale_hours=6,
        now=NOW,
    )
    assert "<script>oops()" not in html and "<img src=x" not in html
    assert 'href="javascript:' not in html
    assert "Failing to crawl" in html and 'href="#"' in html


def test_tracked_timestamps_are_eastern():
    cfg = Config(Profile("dev"), Notifications("test@example.com"), [COMPANY])
    html = tracked.page(
        cfg, State(health={"acme": Health(last_success=NOW)}), stale_hours=6, now=NOW
    )
    assert "Oct 14, 2026 7:00 PM EDT" in html
    assert "23:00 UTC" not in html
