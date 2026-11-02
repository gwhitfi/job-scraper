import re
from datetime import UTC, datetime

from jobscraper import adapters
from jobscraper.models import Company
from jobscraper.storage.state import Posting

# My approve/block decisions, per employer, for postings from job boards.
DECISIONS = {"approved", "blocked"}


def normalize_name(name: str) -> str:
    """Lowercase, no punctuation or legal suffixes: "Fluor Corp." and "FLUOR" match."""
    name = re.sub(r"[^a-z0-9]+", " ", name.lower())
    name = re.sub(r"\b(inc|llc|corp|corporation|co|ltd|company)\b", "", name)
    return " ".join(name.split())


def employer_key(p: Posting) -> str:
    """Normalized employer name; postings without one are judged on their own."""
    name = normalize_name(p.employer)
    return f"employer:{name}" if name else f"posting:{p.key}"


def is_board(p: Posting, companies: dict[str, Company]) -> bool:
    c = companies.get(p.company_id)
    return bool(c and adapters.is_board(c))


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
