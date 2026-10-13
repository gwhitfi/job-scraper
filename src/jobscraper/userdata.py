import json
import os
from datetime import UTC, datetime
from pathlib import Path

# My own marks on postings, kept apart from the runner's state so runs never overwrite them.
FIELDS = {"starred": bool, "hidden": bool, "applied": bool, "note": str}
MAX_NOTE = 5000


def load(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, marks: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(marks, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def update(marks: dict[str, dict], key: str, field: str, value) -> dict:
    """Set one field on one posting's marks and return the updated entry."""
    if field not in FIELDS:
        raise ValueError(f"Unknown field {field!r}")
    if not isinstance(value, FIELDS[field]):
        raise ValueError(f"{field} must be {FIELDS[field].__name__}")
    if field == "note":
        value = value.strip()[:MAX_NOTE]
    entry = marks.setdefault(key, {})
    entry[field] = value
    now = datetime.now(UTC).isoformat(timespec="seconds")
    if field == "applied":
        # Remember when I first marked it applied; clearing it forgets the date.
        if value:
            entry.setdefault("applied_at", now)
        else:
            entry.pop("applied_at", None)
    entry["updated_at"] = now
    return entry
