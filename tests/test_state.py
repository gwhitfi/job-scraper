from datetime import UTC, date, datetime

from jobscraper.storage import state
from jobscraper.storage.state import Health, Posting, State

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


def test_append_archive_merges(tmp_path):
    import json

    p = Posting(
        key="a:1",
        company_id="a",
        source_id="1",
        title="Dev",
        location="SC",
        url="u",
        posted_date=None,
        first_seen=NOW,
        last_seen=NOW,
        status="removed",
        removed_at=NOW,
    )
    state.append_archive({"a:1": p}, tmp_path)
    state.append_archive({"a:2": Posting(**{**p.__dict__, "key": "a:2"})}, tmp_path)
    state.append_archive({}, tmp_path)
    assert set(json.loads((tmp_path / "archive.json").read_text())) == {"a:1", "a:2"}
