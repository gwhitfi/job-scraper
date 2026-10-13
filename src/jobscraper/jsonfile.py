import json
import os
from collections.abc import Callable
from pathlib import Path


def read(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, data: dict, default: Callable | None = None) -> None:
    # Write then rename so a crash mid-write can't leave a truncated file.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    text = json.dumps(data, indent=2, sort_keys=True, default=default) + "\n"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
