"""Local crawl locking, config publication, and rotating data snapshots."""

import fcntl
import os
import tarfile
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from jobscraper import config
from jobscraper.storage import state


@contextmanager
def run_lock(data_dir: Path) -> Iterator[None]:
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / "crawl.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "Another crawl is already running; try again after it finishes."
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def snapshot(data_dir: Path, phase: str, *, keep: int = 60) -> Path:
    """Keep the latest sixty before/after snapshots; never include credentials."""
    directory = data_dir / "backups"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = directory / f"snapshot-{stamp}-{phase}-{uuid4().hex[:8]}.tar.gz"
    files = sorted(data_dir.glob("*.json")) + sorted((data_dir / "state").glob("*.json"))
    files.append(data_dir / "config.toml")
    with tempfile.NamedTemporaryFile(dir=directory, suffix=".tmp", delete=False) as tmp:
        temporary = Path(tmp.name)
    try:
        with tarfile.open(temporary, "w:gz") as archive:
            for path in files:
                if path.is_file() and not path.is_symlink():
                    archive.add(path, arcname=str(path.relative_to(data_dir)), recursive=False)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    for old in sorted(directory.glob("snapshot-*.tar.gz"))[:-keep]:
        old.unlink()
    return destination


def validate(config_path: Path, data_dir: Path) -> None:
    for name in ("postings.json", "health.json"):
        if not (data_dir / "state" / name).is_file():
            raise ValueError(f"Missing local {name}; migrate existing state before scheduling.")
    state.load(data_dir / "state")
    config.load(config_path)


def run(config_path: Path, data_dir: Path, crawl: Callable[[Path, Path], None]) -> None:
    """Run against existing local state, backing up even a failed crawl."""
    with run_lock(data_dir):
        state_dir = data_dir / "state"
        validate(config_path, data_dir)
        snapshot(data_dir, "before")
        published = data_dir / "config.toml"
        if config_path.resolve() != published.resolve():
            temporary = published.with_suffix(".tmp")
            temporary.write_bytes(config_path.read_bytes())
            temporary.replace(published)
        try:
            crawl(published, state_dir)
        finally:
            snapshot(data_dir, "after")
