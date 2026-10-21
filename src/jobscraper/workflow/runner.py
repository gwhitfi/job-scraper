import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

import anthropic
import httpx

from jobscraper import activity, adapters
from jobscraper.classification import classify
from jobscraper.classification.classification_cache import Checkpoint
from jobscraper.classification.usage import Usage
from jobscraper.config import Config
from jobscraper.geography import locations
from jobscraper.models import Company, RawPosting
from jobscraper.scraping import crawl
from jobscraper.storage import employers
from jobscraper.storage.state import Posting, State
from jobscraper.workflow import closures, dedupe, tracker
from jobscraper.workflow.notify import Digest

log = logging.getLogger(__name__)

RETRY_DELAYS = (5.0, 15.0)


@dataclass
class RunResult:
    digest: Digest
    fetched: dict[str, int] = field(default_factory=dict)  # company_id -> postings found
    failed: dict[str, str] = field(default_factory=dict)  # company_id -> error
    usage: Usage = field(default_factory=Usage)  # Claude tokens spent this run
    assessed: list[Posting] = field(default_factory=list)  # snapshots for shadow comparison


def fetch_with_retry(
    company: Company,
    client: httpx.Client,
    sleep: Callable[[float], None] = time.sleep,
    retry_errors: list[str] | None = None,
) -> list[RawPosting]:
    adapter = adapters.get(company.adapter)
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            activity.emit("fetch_attempt", company=company.id, attempt=attempt + 1)
            return adapter(company, client)
        except (httpx.HTTPError, ValueError) as e:
            if retry_errors is not None:
                retry_errors.append(f"{type(e).__name__}: {e}")
            activity.emit(
                "fetch_error",
                company=company.id,
                attempt=attempt + 1,
                error=f"{type(e).__name__}: {e}",
            )
            if attempt == len(RETRY_DELAYS) or isinstance(e, crawl.Blocked | crawl.LimitExceeded):
                raise
            log.warning("%s attempt %d failed: %s", company.id, attempt + 1, e)
            activity.emit("retry", company=company.id, delay_seconds=RETRY_DELAYS[attempt])
            sleep(RETRY_DELAYS[attempt])
    raise AssertionError("unreachable")


def fetch_company(
    company: Company,
    client: httpx.Client,
    regions: dict[str, tuple[str, ...]],
    sleep: Callable[[float], None] = time.sleep,
    extended: tuple[str, ...] = (),
    retry_errors: list[str] | None = None,
) -> tuple[list[RawPosting], list[RawPosting]]:
    """Fetch and location-filter one company; return (kept, dropped).

    Kept postings outside the company's `locations` but inside `extended` get area="extended".
    """
    postings = fetch_with_retry(company, client, sleep, retry_errors)
    if not company.locations:
        return postings, []
    primary = locations.build_filter(company.locations, regions, company.remote)
    kept, outside = locations.apply(postings, primary)
    dropped = outside
    if extended:
        wider = locations.build_filter(extended, regions, remote=False)
        in_wider, dropped = locations.apply(outside, wider)
        kept += [replace(p, area="extended") for p in in_wider]
    activity.emit(
        "location_filter",
        company=company.id,
        kept=len(kept),
        dropped=len(dropped),
        unknown_kept=sum(
            not locations.places(p.location) and not locations.is_remote(p) for p in kept
        ),
        foreign_dropped=sum(
            bool(p.countries) and not any(locations.us_country(c) for c in p.countries)
            for p in dropped
        ),
    )
    for p in locations.possible_gaps(outside, primary):
        log.info(
            "%s: %r in %s is outside `locations` (add the town to a region if it's local)",
            company.id,
            p.title,
            p.location,
        )
    return kept, dropped


def _has_llm_result(p: Posting) -> bool:
    return p.classification is not None and p.classification.get("model") != classify.KEYWORD_MODEL


def _classify_pending(
    state: State,
    cfg: Config,
    llm: anthropic.Anthropic | None,
    usage: Usage,
    checkpoint: Checkpoint | None = None,
    assessed: list[Posting] | None = None,
) -> list[str]:
    """Classify postings that need it; return keys that just became relevant on an LLM retry.

    Postings redone because the rules changed aren't returned: they aren't new jobs.
    """
    scopes = locations.scopes(cfg)
    rules = classify.rules_fingerprint(cfg.profile, cfg.classifier_model)
    pending = [
        p
        for p in state.postings.values()
        if not p.duplicate_of
        and classify.needs_classification(p, rules)
        and (p.company_id not in scopes or scopes[p.company_id].area(p) is not None)
    ]
    if llm is None:
        # Without the LLM, keep stale LLM results rather than downgrading them to keywords.
        pending = [p for p in pending if not _has_llm_result(p)]
    if not pending:
        return []
    was_relevant = {
        p.key: classify.posting_group(p, cfg.profile, scopes.get(p.company_id)) is not None
        for p in pending
    }
    companies = {c.id: c for c in cfg.companies}

    results: dict[str, dict] = {}
    if llm is not None:
        try:
            results = classify.classify_llm(
                pending, cfg.profile, companies, llm, cfg.classifier_model, usage, checkpoint
            )
        except OSError:
            raise  # Do not replace paid results with keyword fallback on a checkpoint failure.
        except Exception:
            log.exception("LLM classification failed, using keywords")
            activity.emit("classification_fallback", reason="LLM failed; using keywords")
    missing = [p for p in pending if p.key not in results and not _has_llm_result(p)]
    results |= classify.classify_keywords(missing, cfg.profile)

    upgraded = []
    for p in pending:
        if p.key not in results:
            continue  # stale LLM result and the LLM failed; retry next run
        prior = p.classification
        p.classification = results[p.key]
        if assessed is not None and _has_llm_result(p):
            assessed.append(replace(p, classification=p.classification.copy()))
        retried = prior is not None and prior.get("model") == classify.KEYWORD_MODEL
        if (
            retried
            and not was_relevant[p.key]
            and classify.posting_group(p, cfg.profile, scopes.get(p.company_id)) is not None
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
    decisions: dict[str, dict] | None = None,
    checkpoint: Checkpoint | None = None,
    verified_closures: dict[str, dict] | None = None,
) -> RunResult:
    """`decisions` are my approve/block calls on job-board employers (employers.json)."""
    now = now or datetime.now(UTC)
    decisions = decisions or {}
    verified_closures = verified_closures or {}
    companies = {c.id: c for c in cfg.companies}
    scopes = locations.scopes(cfg)
    result = RunResult(digest=Digest(health_names=cfg.health_names()))
    new_keys: list[str] = []
    removed_keys: list[str] = []

    for i, company in enumerate(c for c in cfg.companies if c.enabled):
        if i:
            sleep(1.0)
        started = time.monotonic()
        retry_errors: list[str] = []
        try:
            activity.emit(
                "company_started",
                company=company.id,
                name=company.name,
                adapter=adapters.platform(company),
                url=company.url,
            )
            postings, dropped = fetch_company(
                company, http, cfg.regions, sleep, cfg.extended_locations, retry_errors
            )
            tracker.refresh_locations(state, company.id, dropped)
            # Filtering every fetched job is a valid success, not an empty source.
            if tracker.is_suspicious_empty(state, company.id, postings or dropped):
                raise ValueError("returned 0 postings after a non-empty run")
        except Exception as e:  # one site breaking (e.g. a redesign) mustn't stop the others
            if not isinstance(e, httpx.HTTPError | ValueError):
                log.exception("%s: unexpected error", company.id)
            error = f"{type(e).__name__}: {e}"
            result.failed[company.id] = error
            activity.emit(
                "company_failed",
                company=company.id,
                error=error,
                seconds=round(time.monotonic() - started, 2),
            )
            log.warning("%s failed: %s", company.id, error)
            event = tracker.record_failure(state, company.id, error, now)
        else:
            postings = closures.verify_reappearances(
                postings, company, http, state, verified_closures, now
            )
            result.fetched[company.id] = len(postings)
            changes = tracker.apply_results(state, company.id, postings, now)
            activity.emit(
                "company_completed",
                company=company.id,
                fetched=len(postings) + len(dropped),
                kept=len(postings),
                dropped=len(dropped),
                new=len(changes.new),
                removed=len(changes.removed),
                seconds=round(time.monotonic() - started, 2),
            )
            new_keys += changes.new
            removed_keys += changes.removed
            warning = (
                f"Recovered after {len(retry_errors)} failed attempt(s): {retry_errors[-1]}"
                if retry_errors
                else None
            )
            event = tracker.record_success(state, company.id, len(postings), now, warning)
        if event:
            result.digest.health.append(event)

    dedupe.mark_duplicates(state, cfg.companies, adapters.BOARD_ADAPTERS)
    activity.emit("classification_started", mode="llm" if llm else "keywords")
    upgraded = _classify_pending(state, cfg, llm, result.usage, checkpoint, result.assessed)

    activity.emit("classification_completed", calls=result.usage.calls, upgraded=len(upgraded))

    def group(key: str) -> str | None:
        p = state.postings[key]
        if employers.status(p, companies, decisions) == "blocked":
            return None
        return classify.posting_group(p, cfg.profile, scopes.get(p.company_id))

    digest = result.digest
    for key in dict.fromkeys(new_keys + upgraded):
        g = group(key)
        if g is not None:
            getattr(digest, f"new_{g}").append(state.postings[key])
    digest.removed = [state.postings[k] for k in removed_keys if group(k) is not None]
    digest.unverified = {
        p.key for p in digest.new_exact if employers.status(p, companies, decisions) == "unverified"
    }
    announced = {p.key for p in digest.new_exact}
    digest.still_available = [
        p
        for p in state.postings.values()
        if p.status == "active" and p.key not in announced and group(p.key) == "exact"
    ]
    return result
