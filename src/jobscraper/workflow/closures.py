"""Confirmed closures retain history and need readable details before reopening."""

from datetime import datetime

import httpx

from jobscraper import activity
from jobscraper.models import Company, RawPosting
from jobscraper.scraping import crawl, descriptions
from jobscraper.storage.state import Posting, State
from jobscraper.workflow import tracker


def confirm(
    state: State, evidence: dict[str, dict], marks: dict[str, dict], now: datetime
) -> list[str]:
    removed = []
    for key, record in evidence.items():
        posting = state.postings.get(key)
        personal = marks.get(key, {})
        if (
            record.get("status") != "closed"
            or posting is None
            or personal.get("starred")
            or personal.get("applied")
            or posting.status == "removed"
        ):
            continue
        posting.status = "removed"
        posting.removed_at = now
        removed.append(key)
    return removed


def verify_reappearances(
    postings: list[RawPosting],
    company: Company,
    http: httpx.Client,
    state: State,
    evidence: dict[str, dict],
    now: datetime,
) -> list[RawPosting]:
    """A stale search card alone cannot reopen a manually verified closure."""
    candidates = {
        p.source_id
        for p in postings
        if evidence.get(tracker.posting_key(company.id, p.source_id), {}).get("status") == "closed"
        and (
            tracker.posting_key(company.id, p.source_id) not in state.postings
            or state.postings[tracker.posting_key(company.id, p.source_id)].status == "removed"
        )
    }
    if not candidates:
        return postings
    client = crawl.CachedClient(crawl.Client(http, limit=max(20, len(candidates) * 4)))
    kept = []
    for raw in postings:
        if raw.source_id in candidates:
            posting = Posting(
                key=tracker.posting_key(company.id, raw.source_id),
                company_id=company.id,
                source_id=raw.source_id,
                title=raw.title,
                location=raw.location,
                url=raw.url,
                posted_date=raw.posted_date,
                first_seen=now,
                last_seen=now,
            )
            try:
                descriptions.fetch(posting, company, client)
            except (ValueError, httpx.HTTPError) as exc:
                activity.emit(
                    "reopening_unverified",
                    posting=posting.key,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            activity.emit("reopening_verified", posting=posting.key)
        kept.append(raw)
    return kept
