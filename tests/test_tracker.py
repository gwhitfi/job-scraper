from datetime import UTC, date, datetime, timedelta

from jobscraper import tracker
from jobscraper.models import RawPosting
from jobscraper.state import State

T0 = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def raw(source_id: str, title: str = "Dev") -> RawPosting:
    return RawPosting(source_id=source_id, title=title, location="SC", url=f"https://x/{source_id}")


def run(s: State, ids: list[str], hours: int = 0) -> tracker.Changes:
    return tracker.apply_results(s, "co", [raw(i) for i in ids], T0 + timedelta(hours=hours))


def test_first_run_marks_everything_new():
    s = State()
    changes = run(s, ["1", "2"])
    assert changes.new == ["co:1", "co:2"]
    assert s.postings["co:1"].first_seen == T0


def test_unchanged_run_reports_nothing_and_updates_last_seen_daily():
    s = State()
    run(s, ["1"])  # T0 is noon
    assert run(s, ["1"], hours=2) == tracker.Changes()
    assert s.postings["co:1"].first_seen == T0
    assert s.postings["co:1"].last_seen == T0  # same day: unchanged, so state isn't rewritten
    run(s, ["1"], hours=14)  # next day
    assert s.postings["co:1"].last_seen == T0 + timedelta(hours=14)


def test_removal_needs_two_consecutive_misses():
    s = State()
    run(s, ["1", "2"])
    assert run(s, ["1"], hours=1).removed == []
    assert s.postings["co:2"].status == "active"
    changes = run(s, ["1"], hours=2)
    assert changes.removed == ["co:2"]
    assert s.postings["co:2"].status == "removed"
    assert s.postings["co:2"].removed_at == T0 + timedelta(hours=2)
    # Already removed; not reported again.
    assert run(s, ["1"], hours=3).removed == []


def test_reappearing_resets_miss_count():
    s = State()
    run(s, ["1"])
    run(s, [], hours=1)
    run(s, ["1"], hours=2)
    assert run(s, [], hours=3).removed == []


def test_removed_posting_that_returns_is_new_again():
    s = State()
    run(s, ["1"])
    run(s, [], hours=1)
    run(s, [], hours=2)
    changes = run(s, ["1"], hours=3)
    assert changes.new == ["co:1"]
    assert s.postings["co:1"].status == "active"
    assert s.postings["co:1"].removed_at is None
    assert s.postings["co:1"].first_seen == T0


def test_other_companies_untouched():
    s = State()
    tracker.apply_results(s, "other", [raw("1")], T0)
    run(s, [])
    run(s, [], hours=1)
    assert s.postings["other:1"].status == "active"


def test_category_and_summary_stored():
    s = State()
    r = RawPosting("1", "Dev", "SC", "https://x/1", category="IT", summary="Builds things")
    tracker.apply_results(s, "co", [r], T0)
    assert (s.postings["co:1"].category, s.postings["co:1"].summary) == ("IT", "Builds things")


def test_first_posted_date_kept():
    s = State()
    first = RawPosting("1", "Dev", "SC", "u", posted_date=date(2026, 10, 1))
    later = RawPosting("1", "Dev", "SC", "u", posted_date=date(2026, 10, 3))
    tracker.apply_results(s, "co", [first], T0)
    tracker.apply_results(s, "co", [later], T0)
    assert s.postings["co:1"].posted_date == date(2026, 10, 1)


def test_employer_stored():
    s = State()
    tracker.apply_results(s, "co", [RawPosting("1", "Dev", "SC", "u", employer="AFL")], T0)
    assert s.postings["co:1"].employer == "AFL"


def test_title_changes_are_updated_silently():
    s = State()
    run(s, ["1"])
    changes = tracker.apply_results(s, "co", [raw("1", "Senior Dev")], T0)
    assert changes == tracker.Changes()
    assert s.postings["co:1"].title == "Senior Dev"


def test_suspicious_empty():
    s = State()
    assert not tracker.is_suspicious_empty(s, "co", [])  # never succeeded before
    tracker.record_success(s, "co", 5, T0)
    assert tracker.is_suspicious_empty(s, "co", [])
    assert not tracker.is_suspicious_empty(s, "co", [raw("1")])


def test_alert_once_after_three_failures_then_recover():
    s = State()
    events = [tracker.record_failure(s, "co", "timeout", T0 + timedelta(hours=h)) for h in range(5)]
    assert [e.kind if e else None for e in events] == [None, None, "failing", None, None]
    assert s.health["co"].failing_since == T0

    event = tracker.record_success(s, "co", 3, T0 + timedelta(hours=6))
    assert event and event.kind == "recovered"
    assert s.health["co"].consecutive_failures == 0
    assert tracker.record_success(s, "co", 3, T0 + timedelta(hours=7)) is None


def test_recovery_before_alert_is_silent():
    s = State()
    tracker.record_failure(s, "co", "timeout", T0)
    assert tracker.record_success(s, "co", 3, T0) is None


def test_persistent_empty_is_eventually_accepted():
    s = State()
    tracker.record_success(s, "co", 4, T0)
    verdicts = [
        tracker.is_suspicious_empty(s, "co", []) for _ in range(tracker.EMPTY_RUNS_TO_ACCEPT)
    ]
    assert verdicts == [True] * (tracker.EMPTY_RUNS_TO_ACCEPT - 1) + [False]


def test_jobs_returning_resets_empty_streak():
    s = State()
    tracker.record_success(s, "co", 4, T0)
    for _ in range(3):
        tracker.is_suspicious_empty(s, "co", [])
    assert not tracker.is_suspicious_empty(s, "co", [raw("1")])
    assert s.health["co"].empty_streak == 0
    assert tracker.is_suspicious_empty(s, "co", [])  # counting starts over


def test_archive_closed_keeps_recent_and_marked():
    s = State()
    run(s, ["old", "marked", "recent", "open"])
    run(s, ["open"], hours=1)
    run(s, ["open"], hours=2)  # the other three are removed at T0 + 2h
    later = T0 + timedelta(days=tracker.ARCHIVE_AFTER_DAYS, hours=3)
    s.postings["co:recent"].removed_at = later - timedelta(days=5)
    archived = tracker.archive_closed(s, keep={"co:marked"}, now=later)
    assert set(archived) == {"co:old"}
    assert set(s.postings) == {"co:marked", "co:recent", "co:open"}
