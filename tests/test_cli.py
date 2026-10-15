import json
import sys
from datetime import UTC, datetime, timedelta

import pytest

from jobscraper import adapters, cli
from jobscraper.models import RawPosting
from jobscraper.storage import state
from jobscraper.storage.state import Posting, State
from jobscraper.workflow import notify, tracker

CONFIG = """
[profile]
target_role = "software developer"
levels = ["entry", "mid"]
[notifications]
email = "me@example.com"
[[companies]]
id = "acme"
name = "Acme"
adapter = "fake"
url = "https://acme.example/jobs"
added = 2026-01-01
"""


def closed_posting(key: str, days_ago: int) -> Posting:
    now = datetime.now(UTC)
    return Posting(
        key=key,
        company_id="acme",
        source_id=key.split(":")[1],
        title="Old job",
        location="Greenville, SC",
        url="https://acme.example/old",
        posted_date=None,
        first_seen=now - timedelta(days=days_ago + 30),
        last_seen=now - timedelta(days=days_ago),
        status="removed",
        removed_at=now - timedelta(days=days_ago),
    )


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """A config, existing state with two long-closed jobs (one I marked), and a fake site."""
    (tmp_path / "config.toml").write_text(CONFIG)
    old_days = tracker.ARCHIVE_AFTER_DAYS + 30
    state.save(
        State(
            postings={
                "acme:old": closed_posting("acme:old", old_days),
                "acme:mine": closed_posting("acme:mine", old_days),
            }
        ),
        tmp_path / "state",
    )
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "marks.json").write_text(json.dumps({"acme:mine": {"applied": True}}))

    site = [RawPosting("1", "Software Developer I", "Greenville, SC", "https://acme.example/1")]
    monkeypatch.setitem(adapters.ADAPTERS, "fake", lambda company, client: site)
    monkeypatch.setattr(cli, "load_dotenv", lambda *a: None)  # keep the real .env out of tests
    monkeypatch.setenv("SMTP_USER", "me@example.com")
    sent = []
    monkeypatch.setattr(notify, "send", sent.append)

    def run_cli():
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "jobscraper",
                "--config",
                str(tmp_path / "config.toml"),
                "--state-dir",
                str(tmp_path / "state"),
                "run",
                "--no-llm",
                "--data-dir",
                str(tmp_path / "data"),
            ],
        )
        cli.main()

    return tmp_path / "state", sent, run_cli


def test_full_run(setup):
    state_dir, sent, run_cli = setup
    run_cli()

    saved = state.load(state_dir)
    assert "acme:1" in saved.postings
    assert "acme:mine" in saved.postings  # applied, so kept
    assert "acme:old" not in saved.postings
    assert "acme:old" in json.loads((state_dir / "archive.json").read_text())
    assert (state_dir / "usage.json").exists()

    assert [m["Subject"] for m in sent] == ["Job Scraper: 1 new exact match"]
    assert notify.load_outbox(state_dir / "email.json")["pending"] == []


def test_email_failure_still_saves_state(setup, monkeypatch):
    state_dir, _, run_cli = setup

    def broken(msg):
        raise OSError("535 bad credentials")

    monkeypatch.setattr(notify, "send", broken)
    run_cli()  # doesn't raise, so the workflow goes on to save and publish

    assert "acme:1" in state.load(state_dir).postings
    outbox = notify.load_outbox(state_dir / "email.json")
    assert len(outbox["pending"]) == 1 and "535" in outbox["last_error"]

    # Next run: nothing new, but the waiting alert goes out once mail works.
    sent = []
    monkeypatch.setattr(notify, "send", sent.append)
    run_cli()
    assert len(sent) == 1
    assert notify.load_outbox(state_dir / "email.json")["pending"] == []


def test_run_includes_website_added_company(setup):
    from jobscraper.models import Company
    from jobscraper.storage import company_store

    state_dir, sent, run_cli = setup
    c = Company(
        "web-added", "Web Added", "fake", "https://new.example/jobs", datetime.now(UTC).date()
    )
    company_store.add(state_dir.parent / "data" / "companies.json", c)
    run_cli()
    assert "web-added:1" in state.load(state_dir).postings
    assert sent


def test_completed_run_log_has_company_metrics_and_state_save(setup):
    state_dir, sent, run_cli = setup
    run_cli()
    record = json.loads((state_dir / "crawl-history.json").read_text())["runs"][0]
    assert record["status"] == "completed"
    events = record["events"]
    company = next(e for e in events if e["stage"] == "company_completed")
    assert company["company"] == "acme" and company["fetched"] == company["kept"] == 1
    assert company["new"] == 1 and company["removed"] == company["dropped"] == 0
    assert any(e["stage"] == "state_saved" for e in events)
    assert events[-1]["stage"] == "finished"


def test_crashed_run_log_is_preserved(setup, monkeypatch):
    state_dir, _, run_cli = setup

    def fail(*args, **kwargs):
        raise RuntimeError("state save failed")

    monkeypatch.setattr(state, "save", fail)
    with pytest.raises(RuntimeError):
        run_cli()
    record = json.loads((state_dir / "crawl-history.json").read_text())["runs"][0]
    assert record["status"] == "failed"
    assert "state save failed" in str(record["events"])


def test_dry_run_writes_no_logs_or_state(setup, monkeypatch):
    from argparse import Namespace

    state_dir, sent, _ = setup
    before = {p.name: p.read_bytes() for p in state_dir.iterdir()}
    cli.cmd_run(
        Namespace(
            config=state_dir.parent / "config.toml",
            state_dir=state_dir,
            data_dir=state_dir.parent / "data",
            dry_run=True,
            no_llm=True,
            no_email=False,
        )
    )
    assert {p.name: p.read_bytes() for p in state_dir.iterdir()} == before
    assert sent == []
