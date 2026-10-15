import json
from dataclasses import asdict
from datetime import UTC, datetime

import pytest

from jobscraper.geography import repair_locations
from jobscraper.storage import state
from jobscraper.storage.state import Posting, State


def setup(tmp_path):
    (tmp_path / "config.toml").write_text("""
[profile]
target_role = "developer"
[notifications]
email = "test@example.com"
[[companies]]
id = "test"
name = "Test"
adapter = "phenom"
url = "https://example.com"
added = 2026-10-14
""")
    now = datetime.now(UTC)
    p = Posting(
        "test:1",
        "test",
        "1",
        "Developer",
        "Warsaw",
        "https://example.com/1",
        None,
        now,
        now,
        classification={"relevance": "core"},
        missed_runs=1,
    )
    state.save(State(postings={p.key: p}), tmp_path / "state")
    (tmp_path / "marks.json").write_text('{"test:1":{"starred":true,"notes":"keep"}}')
    source = tmp_path / "verified.json"
    source.write_text(
        json.dumps(
            dict(
                company="Test",
                fetched=2,
                postings=[],
                excluded=[
                    dict(source_id="1", location="Warsaw, Poland", countries=["Poland"]),
                    dict(source_id="2", location="Paris, France", countries=["France"]),
                ],
            )
        )
    )
    return source, p


def test_repair_preserves_history_decisions_and_absent_jobs(tmp_path):
    source, before = setup(tmp_path)
    marks = (tmp_path / "marks.json").read_bytes()
    assert repair_locations.repair(tmp_path, "test", source) == 1
    assert state.load(tmp_path / "state").postings[before.key] == before
    assert repair_locations.repair(tmp_path, "test", source, apply=True) == 1
    saved = state.load(tmp_path / "state")
    after = saved.postings[before.key]
    assert after.countries == ("Poland",)
    assert after.location == "Warsaw, Poland"
    assert {k: v for k, v in asdict(after).items() if k not in ("location", "countries")} == {
        k: v for k, v in asdict(before).items() if k not in ("location", "countries")
    }
    assert list(saved.postings) == ["test:1"]
    assert (tmp_path / "marks.json").read_bytes() == marks
    assert len(list((tmp_path / "backups").glob("snapshot-*"))) == 1
    assert repair_locations.repair(tmp_path, "test", source, apply=True) == 0


@pytest.mark.parametrize("mutation", ["company", "fetched", "duplicate"])
def test_repair_rejects_invalid_snapshot_without_writes(tmp_path, mutation):
    source, before = setup(tmp_path)
    raw = json.loads(source.read_text())
    if mutation == "duplicate":
        raw["excluded"][1]["source_id"] = "1"
    else:
        raw[mutation] = "wrong"
    source.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        repair_locations.repair(tmp_path, "test", source, apply=True)
    assert state.load(tmp_path / "state").postings[before.key] == before
    assert not (tmp_path / "backups").exists()
