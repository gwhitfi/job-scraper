from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from jobscraper import jsonfile

# USD per million tokens (input, output), from Anthropic's published API pricing.
PRICES = {
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-opus-5-5": (4.0, 20.0),
}


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, response_usage) -> None:
        self.calls += 1
        self.input_tokens += response_usage.input_tokens or 0
        self.output_tokens += response_usage.output_tokens or 0

    def cost(self, model: str) -> float | None:
        if model not in PRICES:
            return None
        price_in, price_out = PRICES[model]
        return (self.input_tokens * price_in + self.output_tokens * price_out) / 1_000_000


def record(path: Path, run: Usage, model: str, now: datetime) -> dict:
    """Add one run's usage to the monthly totals in usage.json and return the ledger."""
    ledger = jsonfile.read(path) or {"since": now.date().isoformat(), "months": {}}
    if run.calls:
        month = ledger["months"].setdefault(
            now.strftime("%Y-%m"),
            {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
        )
        for k, v in asdict(run).items():
            month[k] += v
        month["cost_usd"] = round(month["cost_usd"] + (run.cost(model) or 0.0), 4)
    jsonfile.write(path, ledger)
    return ledger


def summary(ledger: dict, now: datetime) -> str:
    """e.g. "Claude: $0.42 this month · $1.37 since Oct 12"."""
    months = ledger.get("months", {})
    if not months:
        return ""
    this_month = months.get(now.strftime("%Y-%m"), {}).get("cost_usd", 0.0)
    total = sum(m.get("cost_usd", 0.0) for m in months.values())
    since = datetime.fromisoformat(ledger["since"])
    return f"Claude: ${this_month:.2f} this month · ${total:.2f} since {since:%b %-d}"
