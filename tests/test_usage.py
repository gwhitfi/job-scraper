from datetime import UTC, datetime
from types import SimpleNamespace

from jobscraper import usage

NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)


def test_cost():
    u = usage.Usage()
    u.add(SimpleNamespace(input_tokens=1_000_000, output_tokens=100_000))
    assert u.calls == 1
    assert u.cost("claude-sonnet-5-5") == 3.0  # $2 in + $1 out
    assert u.cost("some-unknown-model") is None


def test_record_accumulates_by_month(tmp_path):
    path = tmp_path / "usage.json"
    run = usage.Usage(calls=2, input_tokens=500_000, output_tokens=50_000)  # $1.50
    usage.record(path, run, "claude-sonnet-5-5", NOW)
    ledger = usage.record(path, run, "claude-sonnet-5-5", NOW)
    oct = ledger["months"]["2026-10"]
    assert (oct["calls"], oct["cost_usd"]) == (4, 3.0)
    nov = usage.record(path, run, "claude-sonnet-5-5", datetime(2026, 11, 2, tzinfo=UTC))
    assert nov["months"]["2026-11"]["cost_usd"] == 1.5
    assert usage.summary(nov, datetime(2026, 11, 2, tzinfo=UTC)) == (
        "Claude: $1.50 this month · $4.50 since Oct 13"
    )


def test_runs_without_calls_add_nothing(tmp_path):
    ledger = usage.record(tmp_path / "usage.json", usage.Usage(), "claude-sonnet-5-5", NOW)
    assert ledger["months"] == {}
    assert usage.summary(ledger, NOW) == ""
