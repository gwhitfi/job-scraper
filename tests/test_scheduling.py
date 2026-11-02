import argparse
import json
import tarfile

import pytest
from test_cli import CONFIG

from jobscraper import cli
from jobscraper.storage import state
from jobscraper.workflow import scheduling


@pytest.fixture
def local_data(tmp_path):
    data = tmp_path / "data"
    state.save(state.State(), data / "state")
    (data / "marks.json").write_text('{"saved-job": {"starred": true}}')
    (data / "config.toml").write_text(CONFIG + "\n# previous config")
    config_path = tmp_path / "config.toml"
    config_path.write_text(CONFIG)
    return config_path, data


def test_overlapping_crawl_is_rejected_and_lock_releases(tmp_path):
    with (
        scheduling.run_lock(tmp_path),
        pytest.raises(RuntimeError, match="already running"),
        scheduling.run_lock(tmp_path),
    ):
        pytest.fail("overlapping crawl acquired lock")
    with scheduling.run_lock(tmp_path):
        pass


def test_local_run_keeps_before_and_after_snapshots(local_data):
    config_path, data = local_data
    marks = (data / "marks.json").read_bytes()

    def crawl(published, state_dir):
        assert published == data / "config.toml"
        assert published.read_bytes() == config_path.read_bytes()
        assert state_dir == data / "state"
        (state_dir / "usage.json").write_text('{"calls": 1}')

    scheduling.run(config_path, data, crawl)
    snapshots = sorted((data / "backups").glob("*.tar.gz"))
    assert len(snapshots) == 2
    with tarfile.open(snapshots[0]) as before, tarfile.open(snapshots[1]) as after:
        assert "state/usage.json" not in before.getnames()
        assert json.load(after.extractfile("state/usage.json")) == {"calls": 1}
        assert b"previous config" in before.extractfile("config.toml").read()
        assert after.extractfile("marks.json").read() == marks
    assert (data / "marks.json").read_bytes() == marks


def test_failed_crawl_preserves_trace_in_backup_and_unlocks(local_data):
    config_path, data = local_data

    def crawl(*args):
        (data / "state" / "crawl-current.json").write_text('{"status": "failed"}')
        raise RuntimeError("crawl failed")

    with pytest.raises(RuntimeError, match="crawl failed"):
        scheduling.run(config_path, data, crawl)
    last = sorted((data / "backups").glob("*.tar.gz"))[-1]
    with tarfile.open(last) as backup:
        assert json.load(backup.extractfile("state/crawl-current.json"))["status"] == "failed"
    with scheduling.run_lock(data):
        pass


def test_backup_retention_excludes_secrets_and_unrelated_files(local_data):
    _, data = local_data
    (data / ".env").write_text("fixture-secret")
    (data / "backups").mkdir()
    (data / "backups" / "migration.tar.gz").write_bytes(b"keep")
    for _ in range(3):
        last = scheduling.snapshot(data, "test", keep=2)
    assert len(list((data / "backups").glob("snapshot-*.tar.gz"))) == 2
    assert (data / "backups" / "migration.tar.gz").read_bytes() == b"keep"
    with tarfile.open(last) as backup:
        assert ".env" not in backup.getnames()
        assert all(not name.startswith("backups/") for name in backup.getnames())


def test_scheduler_refuses_to_start_from_missing_state(tmp_path):
    with pytest.raises(ValueError, match="migrate existing state"):
        scheduling.run(tmp_path / "config.toml", tmp_path, lambda *a: pytest.fail("ran crawl"))


def test_preflight_checks_credentials_without_exposing_them(local_data, monkeypatch, capsys):
    config_path, data = local_data
    args = argparse.Namespace(config=config_path, data_dir=data, check=True)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        cli.cmd_scheduled_run(args)
    for key in ("ANTHROPIC_API_KEY", "SMTP_USER", "SMTP_APP_PASSWORD"):
        monkeypatch.setenv(key, "fixture-secret")
    cli.cmd_scheduled_run(args)
    output = capsys.readouterr().out
    assert "ready" in output and "fixture-secret" not in output
    assert not (data / "backups").exists()


def test_company_name_overrides_are_backed_up(local_data):
    _, data = local_data
    (data / "company-names.json").write_text('{"acme": "New name"}')
    snapshot = scheduling.snapshot(data, "test")
    with tarfile.open(snapshot) as backup:
        assert json.load(backup.extractfile("company-names.json")) == {"acme": "New name"}
