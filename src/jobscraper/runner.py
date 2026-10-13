import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import anthropic
import httpx

from jobscraper import adapters, classify, locations, tracker
from jobscraper.config import Config
from jobscraper.models import Company, RawPosting
from jobscraper.notify import Digest
from jobscraper.state import State

log = logging.getLogger(__name__)

FETCH_ATTEMPTS = 3
RETRY_DELAYS = (5.0, 15.0)


@dataclass
class RunResult:
    digest: Digest
    fetched: dict[str, int] = field(default_factory=dict)  # company_id -> postings found
    failed: dict[str, str] = field(default_factory=dict)  # company_id -> error


def fetch_with_retry(
    company: Company, client: httpx.Client, sleep: Callable[[float], None] = time.sleep
) -> list[RawPosting]:
    adapter = adapters.get(company.adapter)
    for attempt in range(FETCH_ATTEMPTS):
        try:
            return adapter(company, client)
        except (httpx.HTTPError, ValueError) as e:
            if attempt == FETCH_ATTEMPTS - 1:
                raise
            log.warning("%s attempt %d failed: %s", company.id, attempt + 1, e)
            sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
    raise AssertionError("unreachable")


def fetch_company(
    company: Company,
    client: httpx.Client,
    regions: dict[str, tuple[str, ...]],
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[RawPosting], list[RawPosting]]:
    """Fetch and location-filter one company; return (kept, dropped)."""
    postings = fetch_with_retry(company, client, sleep)
    if not company.locations:
        return postings, []
    f = locations.build_filter(company.locations, regions, company.remote)
    kept, dropped = locations.apply(postings, f)
    for p in locations.possible_gaps(dropped, f):
        log.info(
            "%s: dropped %r in %s (add the city to a region if it's local)",
            company.id,
            p.title,
            p.location,
        )
    return kept, dropped


def _has_llm_result(p) -> bool:
    return p.classification is not None and p.classification.get("model") != classify.KEYWORD_MODEL


def _classify_pending(state: State, cfg: Config, llm: anthropic.Anthropic | None) -> list[str]:
    """Classify postings that need it; return keys that just became relevant on an LLM retry.

    Postings redone because the rules changed aren't returned: they aren't new jobs.
    """
    rules = classify.rules_fingerprint(cfg.profile, cfg.classifier_model)
    pending = [p for p in state.postings.values() if classify.needs_classification(p, rules)]
    if llm is None:
        # Without the LLM, keep stale LLM results rather than downgrading them to keywords.
        pending = [p for p in pending if not _has_llm_result(p)]
    if not pending:
        return []
    was_relevant = {p.key: classify.is_relevant(p.classification, cfg.profile) for p in pending}
    companies = {c.id: c for c in cfg.companies}

    results: dict[str, dict] = {}
    if llm is not None:
        try:
            results = classify.classify_llm(
                pending, cfg.profile, companies, llm, cfg.classifier_model
            )
        except (anthropic.APIError, RuntimeError) as e:
            log.warning("LLM classification failed, using keywords: %s", e)
    missing = [p for p in pending if p.key not in results and not _has_llm_result(p)]
    results |= classify.classify_keywords(missing, cfg.profile)

    upgraded = []
    for p in pending:
        if p.key not in results:
            continue  # stale LLM result and the LLM failed; retry next run
        prior = p.classification
        p.classification = results[p.key]
        retried = prior is not None and prior.get("model") == classify.KEYWORD_MODEL
        if (
            retried
            and not was_relevant[p.key]
            and classify.is_relevant(p.classification, cfg.profile)
        ):
            upgraded.append(p.key)
    return upgraded


def run(
    cfg: Config,
    state: State,
    http: httpx.Client,
    llm: anthropic.Anthropic | None,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> RunResult:
    now = now or datetime.now(UTC)
    result = RunResult(digest=Digest())
    new_keys: list[str] = []
    removed_keys: list[str] = []

    for i, company in enumerate(c for c in cfg.companies if c.enabled):
        if i:
            sleep(1.0)
        try:
            postings, _ = fetch_company(company, http, cfg.regions, sleep)
            if tracker.is_suspicious_empty(state, company.id, postings):
                raise ValueError("returned 0 postings after a non-empty run")
        except (httpx.HTTPError, ValueError) as e:
            error = f"{type(e).__name__}: {e}"
            result.failed[company.id] = error
            log.warning("%s failed: %s", company.id, error)
            event = tracker.record_failure(state, company.id, error, now)
        else:
            result.fetched[company.id] = len(postings)
            changes = tracker.apply_results(state, company.id, postings, now)
            new_keys += changes.new
            removed_keys += changes.removed
            event = tracker.record_success(state, company.id, len(postings), now)
        if event:
            result.digest.health.append(event)

    upgraded = _classify_pending(state, cfg, llm)

    def group(key: str) -> str | None:
        return classify.match_group(state.postings[key].classification, cfg.profile)

    digest = result.digest
    for key in dict.fromkeys(new_keys + upgraded):
        g = group(key)
        if g is not None:
            getattr(digest, f"new_{g}").append(state.postings[key])
    digest.removed = [state.postings[k] for k in removed_keys if group(k) is not None]
    announced = {p.key for p in digest.new_exact}
    digest.still_available = [
        p
        for p in state.postings.values()
        if p.status == "active" and p.key not in announced and group(p.key) == "exact"
    ]
    return result
