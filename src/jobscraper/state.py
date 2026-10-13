from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime
from pathlib import Path

from jobscraper import jsonfile

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
    area: str = "primary"
    duplicate_of: str | None = None  # key of the same job on a company's own site

    def source_label(self, company_name: str) -> str:
        """e.g. "AFL (via Move Upstate SC)" for job boards, else just the company name."""
        return f"{self.employer} (via {company_name})" if self.employer else company_name


@dataclass
class Health:
    consecutive_failures: int = 0
    last_success: datetime | None = None
    last_error: str | None = None
    last_count: int | None = None
    failing_since: datetime | None = None
    alerted: bool = False
    # Consecutive empty results after a non-empty run; accepted as real at EMPTY_RUNS_TO_ACCEPT.
    empty_streak: int = 0


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


def load(state_dir: Path = DEFAULT_DIR) -> State:
    postings = jsonfile.read(state_dir / "postings.json")
    health = jsonfile.read(state_dir / "health.json")
    return State(
        postings={k: _decode(Posting, v) for k, v in postings.items()},
        health={k: _decode(Health, v) for k, v in health.items()},
    )


def save(state: State, state_dir: Path = DEFAULT_DIR) -> None:
    postings = {k: asdict(v) for k, v in state.postings.items()}
    health = {k: asdict(v) for k, v in state.health.items()}
    jsonfile.write(state_dir / "postings.json", postings, default=_encode)
    jsonfile.write(state_dir / "health.json", health, default=_encode)
