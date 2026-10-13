import json
import os
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime
from pathlib import Path

DEFAULT_DIR = Path("state")


@dataclass
class Posting:
    key: str
    company_id: str
    source_id: str
    title: str
    location: str
    url: str
    posted_date: date | None
    first_seen: datetime
    last_seen: datetime
    status: str = "active"  # "active" | "removed"
    removed_at: datetime | None = None
    # Consecutive successful runs this posting was missing from; removal happens at 2.
    missed_runs: int = 0
    classification: dict | None = None
    category: str = ""
    summary: str = ""
    employer: str = ""


@dataclass
class Health:
    consecutive_failures: int = 0
    last_success: datetime | None = None
    last_error: str | None = None
    last_count: int | None = None
    failing_since: datetime | None = None
    alerted: bool = False


@dataclass
class State:
    postings: dict[str, Posting] = field(default_factory=dict)
    health: dict[str, Health] = field(default_factory=dict)


_DATETIMES = {"first_seen", "last_seen", "removed_at", "last_success", "failing_since"}


def _decode(cls, raw: dict):
    known = {f.name for f in fields(cls)}
    values = {}
    for k, v in raw.items():
        if k not in known:
            continue
        if v is not None and k in _DATETIMES:
            v = datetime.fromisoformat(v)
        elif v is not None and k == "posted_date":
            v = date.fromisoformat(v)
        values[k] = v
    return cls(**values)


def _encode(value):
    if isinstance(value, date | datetime):
        return value.isoformat()
    raise TypeError(f"Can't serialize {type(value).__name__}")


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, data: dict) -> None:
    # Write then rename so a crash mid-write can't leave a truncated file.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, sort_keys=True, default=_encode) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


def load(state_dir: Path = DEFAULT_DIR) -> State:
    postings = _read(state_dir / "postings.json")
    health = _read(state_dir / "health.json")
    return State(
        postings={k: _decode(Posting, v) for k, v in postings.items()},
        health={k: _decode(Health, v) for k, v in health.items()},
    )


def save(state: State, state_dir: Path = DEFAULT_DIR) -> None:
    _write(state_dir / "postings.json", {k: asdict(v) for k, v in state.postings.items()})
    _write(state_dir / "health.json", {k: asdict(v) for k, v in state.health.items()})
