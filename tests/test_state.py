from datetime import UTC, date, datetime

from jobscraper import state
from jobscraper.state import Health, Posting, State

NOW = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)


def test_round_trip(tmp_path):
    s = State(
        postings={
            "a:1": Posting(
                key="a:1",
                company_id="a",
                source_id="1",
                title="Dev",
                location="Greenville, SC",
                url="https://x/1",
                posted_date=date(2026, 10, 1),
                first_seen=NOW,
                last_seen=NOW,
                classification={"relevance": "core", "level": "entry"},
            )
        },
        health={"a": Health(last_success=NOW, last_count=1)},
    )
    state.save(s, tmp_path)
    assert state.load(tmp_path) == s


def test_load_missing_dir_is_empty(tmp_path):
    assert state.load(tmp_path / "nope") == State()
