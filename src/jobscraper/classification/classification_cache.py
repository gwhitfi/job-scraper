"""Resume completed classifier requests without advancing posting/removal state."""

import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from jobscraper.classification import usage
from jobscraper.storage import jsonfile


class Checkpoint:
    def __init__(self, path: Path, run_id: str, model: str, started: datetime):
        self.path = path
        self.ledger = path.with_name("usage.json")
        self.data = jsonfile.read(path) or {"results": {}}
        self.run_id, self.model, self.started = run_id, model, started
        self.account()  # Recover a crash between saving results and recording their cost.

    @staticmethod
    def signature(prompt: str, rules: str) -> str:
        return hashlib.sha256(json.dumps([prompt, rules]).encode()).hexdigest()

    def get(self, key: str, signature: str) -> dict | None:
        cached = self.data["results"].get(key)
        return cached["result"] if cached and cached["signature"] == signature else None

    def save(self, results: dict, signatures: dict[str, str], spent: usage.Usage) -> None:
        for key, result in results.items():
            self.data["results"][key] = {"signature": signatures[key], "result": result}
        self.data["accounting"] = {
            "id": self.run_id,
            "model": self.model,
            "started": self.started.isoformat(),
            "usage": asdict(spent),
        }
        jsonfile.write(self.path, self.data)
        self.account()

    def account(self) -> None:
        record = self.data.get("accounting")
        if record:
            usage.record(
                self.ledger,
                usage.Usage(**record["usage"]),
                record["model"],
                datetime.fromisoformat(record["started"]),
                record["id"],
            )
            del self.data["accounting"]
            jsonfile.write(self.path, self.data)

    def finish(self, applied: dict | None = None) -> None:
        self.data["results"] = {
            key: cached
            for key, cached in self.data["results"].items()
            if applied is not None
            and (key not in applied or applied[key].classification != cached["result"])
        }
        jsonfile.write(self.path, self.data)
