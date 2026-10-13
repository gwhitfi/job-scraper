from dataclasses import dataclass, field
from datetime import datetime

from jobscraper.models import RawPosting
from jobscraper.state import Health, Posting, State

MISSES_BEFORE_REMOVAL = 2
FAILURES_BEFORE_ALERT = 3
EMPTY_RUNS_TO_ACCEPT = 6  # ~12 hours of empty results means the company really has no jobs


@dataclass
class Changes:
    new: list[str] = field(default_factory=list)  # includes removed postings that came back
    removed: list[str] = field(default_factory=list)


@dataclass
class HealthEvent:
    company_id: str
    kind: str  # "failing" | "recovered"
    health: Health


def posting_key(company_id: str, source_id: str) -> str:
    return f"{company_id}:{source_id}"


def apply_results(
    state: State, company_id: str, results: list[RawPosting], now: datetime
) -> Changes:
    """Merge one successful fetch into state and return what changed."""
    changes = Changes()
    seen = set()

    for raw in results:
        key = posting_key(company_id, raw.source_id)
        seen.add(key)
        existing = state.postings.get(key)
        if existing is None:
            state.postings[key] = Posting(
                key=key,
                company_id=company_id,
                source_id=raw.source_id,
                title=raw.title,
                location=raw.location,
                url=raw.url,
                posted_date=raw.posted_date,
                category=raw.category,
                summary=raw.summary,
                employer=raw.employer,
                area=raw.area,
                first_seen=now,
                last_seen=now,
            )
            changes.new.append(key)
            continue

        if existing.status == "removed":
            existing.status = "active"
            existing.removed_at = None
            changes.new.append(key)
        existing.title = raw.title
        existing.location = raw.location
        existing.url = raw.url
        # Keep the first date seen; boards that only say "3 days ago" would drift otherwise.
        existing.posted_date = existing.posted_date or raw.posted_date
        existing.category = raw.category
        existing.summary = raw.summary
        existing.employer = raw.employer
        existing.area = raw.area
        existing.last_seen = now
        existing.missed_runs = 0

    for key, posting in state.postings.items():
        if posting.company_id != company_id or posting.status != "active" or key in seen:
            continue
        posting.missed_runs += 1
        if posting.missed_runs >= MISSES_BEFORE_REMOVAL:
            posting.status = "removed"
            posting.removed_at = now
            changes.removed.append(key)

    return changes


def is_suspicious_empty(state: State, company_id: str, results: list[RawPosting]) -> bool:
    """Zero results after a non-empty run is more likely a broken page than every job closing.

    Counts consecutive empty runs and stops treating them as suspicious once they've
    persisted long enough to be believable.
    """
    health = state.health.setdefault(company_id, Health())
    if results or not health.last_count:
        health.empty_streak = 0
        return False
    health.empty_streak += 1
    return health.empty_streak < EMPTY_RUNS_TO_ACCEPT


def record_success(state: State, company_id: str, count: int, now: datetime) -> HealthEvent | None:
    health = state.health.setdefault(company_id, Health())
    recovered = health.alerted
    health.consecutive_failures = 0
    health.last_success = now
    health.last_error = None
    health.last_count = count
    health.failing_since = None
    health.alerted = False
    health.empty_streak = 0
    return HealthEvent(company_id, "recovered", health) if recovered else None


def record_failure(state: State, company_id: str, error: str, now: datetime) -> HealthEvent | None:
    health = state.health.setdefault(company_id, Health())
    health.consecutive_failures += 1
    health.last_error = error
    health.failing_since = health.failing_since or now
    if health.consecutive_failures >= FAILURES_BEFORE_ALERT and not health.alerted:
        health.alerted = True
        return HealthEvent(company_id, "failing", health)
    return None
