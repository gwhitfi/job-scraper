import re
from datetime import UTC, datetime
from pathlib import Path

from jobscraper import jsonfile
from jobscraper.adapters import BOARD_ADAPTERS
from jobscraper.models import Company
from jobscraper.state import Posting

# My approve/block decisions, per employer, for postings from job boards.
DECISIONS = {"approved", "blocked"}


def employer_key(p: Posting) -> str:
    """Normalized employer name; postings without one are judged on their own."""
    name = re.sub(r"[^a-z0-9]+", " ", p.employer.lower()).strip()
    name = re.sub(r"\b(inc|llc|corp|corporation|co|ltd|company)\b", "", name)
    name = " ".join(name.split())
    return f"employer:{name}" if name else f"posting:{p.key}"


def is_board(p: Posting, companies: dict[str, Company]) -> bool:
    c = companies.get(p.company_id)
    return bool(c and c.adapter in BOARD_ADAPTERS)


def status(p: Posting, companies: dict[str, Company], decisions: dict[str, dict]) -> str:
    """One of: trusted (a company I added), approved, blocked, unverified, ok."""
    if not is_board(p, companies):
        return "trusted"
    decision = decisions.get(employer_key(p), {}).get("status")
    if decision in DECISIONS:
        return decision
    if (p.classification or {}).get("employer_check") == "verify":
        return "unverified"
    return "ok"


def load(path: Path) -> dict[str, dict]:
    return jsonfile.read(path)


def save(path: Path, decisions: dict[str, dict]) -> None:
    jsonfile.write(path, decisions)


def decide(decisions: dict[str, dict], key: str, name: str, decision: str | None) -> None:
    """Record approve/block for an employer; None clears it back to Claude's call."""
    if decision is None:
        decisions.pop(key, None)
        return
    if decision not in DECISIONS:
        raise ValueError(f"Unknown decision {decision!r}")
    decisions[key] = {
        "status": decision,
        "name": name,
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
