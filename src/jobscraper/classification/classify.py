import hashlib
import json
import logging
import re
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import Literal

import anthropic
from pydantic import BaseModel

from jobscraper import activity, adapters
from jobscraper.classification import policy, salaries
from jobscraper.classification.classification_cache import Checkpoint
from jobscraper.classification.usage import Usage
from jobscraper.config import DEFAULT_MODEL, Profile
from jobscraper.geography import locations
from jobscraper.models import Company
from jobscraper.storage.state import Posting

log = logging.getLogger(__name__)

BATCH_SIZE = 50  # keeps each reply well under max_tokens
KEYWORD_MODEL = "keywords"  # marks fallback results so they get retried with the LLM

Relevance = Literal["core", "adjacent", "maybe", "none", "industrial"]
Level = Literal["intern", "entry", "mid", "senior", "lead", "unknown"]
Score = Literal[1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
EmployerCheck = Literal["ok", "verify"]


class Result(BaseModel):
    # Fields are generated in this order, so the reason comes before the verdict.
    id: str
    reason: str
    score: Score
    relevance: Relevance
    level: Level
    employer_note: str
    employer_check: EmployerCheck
    salary: salaries.Salary | None = None


class Results(BaseModel):
    results: list[Result]


SYSTEM = (
    "You screen job postings for one job seeker.\n\n"
    + policy.GUIDANCE
    + "\nRelevance choices:\n"
    + "\n".join(f'- "{name}": {description}' for name, description in policy.CRITERIA.items())
    + "\n\n"
    + """\
score: how similar the work is to the target role, 1-10, ignoring seniority:
- 10: the target role or an equivalent title
- 7-9: mostly the same work (e.g. for software developer: QA automation, DevOps, full-stack)
- 4-6: related technical work (e.g. data engineer, IT support, BI developer)
- 1-3: loosely related or unrelated (e.g. product analyst, tech sales, warehouse)

level:
- "intern": internships, co-ops, summer programs
- "entry": junior, associate, "I", new grad, trainee
- "mid": no seniority marker, or "II"
- "senior": senior, sr, "III", staff
- "lead": lead, principal, manager, director, head of
- "unknown": the title gives no usable signal

employer_check (only matters for postings marked "via" a job board; otherwise answer "ok"):
- "verify": you don't recognize the employer as a real organization, no employer is named, or \
the posting shows scam signs (reshipping, "payment processing", upfront fees, chat-app \
interviews, pay far above the norm for easy remote work, vague duties)
- "ok": a recognizable real employer with an ordinary posting
employer_note: at most 12 words on why, or "" when "ok".

For each posting, first write a reason (at most 20 words) saying what kind of work the job \
is, then give score, relevance, level, employer_note, and employer_check. Return one result \
per posting, using its id."""
)


SUMMARY_CHARS = 300


def company_label(p: Posting, companies: dict[str, Company]) -> str:
    c = companies.get(p.company_id)
    if c is None:
        return p.employer or p.company_id
    if adapters.is_board(c) and not p.employer:
        return f"Unknown employer via {c.name}"
    if p.employer or not c.about:
        return p.source_label(c.name)
    return f"{c.name} ({c.about})"


def _prompt(postings: list[Posting], profile: Profile, companies: dict[str, Company]) -> str:
    lines = [
        f"Target role: {profile.target_role}",
        f"Equivalent titles: {', '.join(profile.core_examples) or 'none listed'}",
        f"Acceptable adjacent roles: {', '.join(profile.adjacent_examples) or 'none listed'}",
        f"Excluded roles: {', '.join(profile.exclude_examples) or 'none listed'}",
        f"Helpful keywords: {', '.join(profile.keywords) or 'none listed'}",
        f"Screening rules version: {profile.rules_version}",
        f"Screening rules: {profile.screening_rules or 'Use the relevance definitions'}",
        f"Industrial/controls manual review: {', '.join(profile.review_examples) or 'none listed'}",
        "",
        "Postings:",
    ]
    for p in postings:
        line = f"- id={p.key} | {p.title} | {company_label(p, companies)} | {p.location}"
        line += f" | area: {p.area}"
        if p.category:
            line += f" | category: {p.category}"
        if p.summary:
            line += f" | summary: {p.summary[:SUMMARY_CHARS]}"
        lines.append(line)
    return "\n".join(lines)


# Profile fields that go into the prompt. `levels` and `mode` only filter what's shown, so
# changing them shouldn't pay for reclassifying everything.
PROMPT_FIELDS = (
    "target_role",
    "core_examples",
    "adjacent_examples",
    "exclude_examples",
    "keywords",
    "screening_rules",
    "review_examples",
    "rules_version",
)


def rules_fingerprint(profile: Profile, model: str) -> str:
    # Salary instructions are separate so enrichment does not invalidate relevance reviews.
    """Changes when anything Claude is told changes, so stale results get redone."""
    seen = {k: v for k, v in asdict(profile).items() if k in PROMPT_FIELDS}
    blob = json.dumps([seen, SYSTEM, model], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def classify_llm(
    postings: list[Posting],
    profile: Profile,
    companies: dict[str, Company],
    client: anthropic.Anthropic,
    model: str = DEFAULT_MODEL,
    usage: Usage | None = None,
    checkpoint: Checkpoint | None = None,
) -> dict[str, dict]:
    """Classify in batches; postings that can't be classified are left out for a retry."""

    usage = usage if usage is not None else Usage()
    rules = rules_fingerprint(profile, model)
    signatures = {
        p.key: Checkpoint.signature(_prompt([p], profile, companies), rules) for p in postings
    }
    results = {
        p.key: cached
        for p in postings
        if checkpoint and (cached := checkpoint.get(p.key, signatures[p.key])) is not None
    }
    pending = [p for p in postings if p.key not in results]
    total, completed = len(postings), len(results)
    activity.emit(
        "classification_plan",
        total=total,
        reused=completed,
        pending=len(pending),
        batches=(len(pending) + BATCH_SIZE - 1) // BATCH_SIZE,
    )
    log.info(
        "Classification: %d jobs, %d restored from checkpoint, %d pending",
        total,
        completed,
        len(pending),
    )

    def attempt(batch: list[Posting]) -> dict[str, dict]:
        """If a batch's content breaks the reply, split it to isolate the bad posting."""
        nonlocal completed
        started = time.monotonic()
        activity.emit(
            "classification_batch_started", jobs=len(batch), completed=completed, total=total
        )
        try:
            out = classify_batch(batch, profile, companies, client, model, usage)
        except Exception as e:
            # Even unusable responses may have billed usage. Never retry a disk failure as
            # if it were bad classifier output.
            if checkpoint:
                checkpoint.save({}, signatures, usage)
            activity.emit(
                "classification_batch_failed", jobs=len(batch), error=f"{type(e).__name__}: {e}"
            )
            if _unavailable(e):
                raise
            if len(batch) == 1:
                log.warning("Couldn't classify %s: %s: %s", batch[0].key, type(e).__name__, e)
                return {}
            log.warning("Batch of %d failed (%s: %s); splitting", len(batch), type(e).__name__, e)
            mid = len(batch) // 2
            return attempt(batch[:mid]) | attempt(batch[mid:])
        if checkpoint:
            checkpoint.save(out, signatures, usage)
        results.update(out)  # Keep successful split batches even if a later half is unavailable.
        completed += len(out)
        activity.emit(
            "classification_progress",
            completed=completed,
            total=total,
            calls=usage.calls,
            seconds=round(time.monotonic() - started, 2),
        )
        log.info(
            "Classification: %d/%d jobs complete (%d API calls)", completed, total, usage.calls
        )
        return out

    for start in range(0, len(pending), BATCH_SIZE):
        try:
            results |= attempt(pending[start : start + BATCH_SIZE])
        except Exception as e:
            if not _unavailable(e):
                raise
            # Claude itself is down or refusing us; more calls this run would fail too.
            log.warning("Claude unavailable, skipping %d postings: %s", len(pending) - start, e)
            break
    return results


def _unavailable(e: Exception) -> bool:
    """Outages, rate limits, and credential problems, as opposed to a bad batch."""
    if isinstance(e, anthropic.APIConnectionError):
        return True
    return isinstance(e, anthropic.APIStatusError) and (
        e.status_code >= 500 or e.status_code in (401, 403, 429)
    )


def classify_batch(
    postings: list[Posting],
    profile: Profile,
    companies: dict[str, Company],
    client: anthropic.Anthropic,
    model: str = DEFAULT_MODEL,
    usage: Usage | None = None,
) -> dict[str, dict]:
    """Classify postings in one request. Postings missing from the reply are left out."""
    response = client.messages.parse(
        model=model,
        max_tokens=16000,
        system=SYSTEM
        + salaries.INSTRUCTIONS
        + (
            f"\nPay eligibility: relevance must be "
            f"{'core only' if profile.mode == 'strict' else 'core, adjacent, or maybe'}; "
            f"level must be unknown or one of {', '.join(profile.levels)}. "
            "For area=extended only core relevance qualifies. All other jobs need salary=null."
        ),
        messages=[{"role": "user", "content": _prompt(postings, profile, companies)}],
        output_format=Results,
    )
    if usage is not None and getattr(response, "usage", None):
        usage.add(response.usage)  # billed even if the reply turns out unusable
    if response.stop_reason != "end_turn" or response.parsed_output is None:
        raise RuntimeError(f"Classifier stopped early: {response.stop_reason}")

    now = datetime.now(UTC).isoformat()
    wanted = {p.key for p in postings}
    by_key = {p.key: p for p in postings}
    rules = rules_fingerprint(profile, model)
    return {
        r.id: {
            "relevance": r.relevance,
            "level": r.level,
            "reason": r.reason,
            "score": r.score,
            "employer_check": r.employer_check,
            "employer_note": r.employer_note,
            "model": model,
            "rules": rules,
            "policy_version": profile.rules_version,
            "classified_at": now,
            **(
                {"salary": salaries.serialize(r.salary, model)}
                if salaries.eligible(
                    replace(
                        by_key[r.id], classification={"relevance": r.relevance, "level": r.level}
                    ),
                    profile,
                )
                else {}
            ),
        }
        for r in response.parsed_output.results
        if r.id in wanted
    }


_LEVEL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("intern", re.compile(r"\b(intern|internship|co-?op|summer)\b", re.I)),
    ("lead", re.compile(r"\b(lead|principal|manager|director|head of)\b", re.I)),
    ("senior", re.compile(r"\b(senior|sr\.?|staff|iii)\b", re.I)),
    ("entry", re.compile(r"\b(junior|jr\.?|associate|entry|trainee|new grad|i)\b", re.I)),
]


def _words_pattern(terms: tuple[str, ...]) -> re.Pattern:
    """Matches any term as whole words (plural allowed), so "dev" doesn't match "Device"."""
    alternatives = "|".join(re.escape(t.lower()) for t in terms if t.strip())
    return re.compile(rf"\b(?:{alternatives})s?\b" if alternatives else r"(?!)", re.I)


def classify_keywords(postings: list[Posting], profile: Profile) -> dict[str, dict]:
    """Rough title-only fallback for when the LLM is unavailable."""
    core = _words_pattern((profile.target_role, *profile.core_examples, *profile.keywords))
    adjacent = _words_pattern(profile.adjacent_examples)
    excluded = _words_pattern(profile.exclude_examples)
    now = datetime.now(UTC).isoformat()
    out = {}
    for p in postings:
        level = next((lvl for lvl, pat in _LEVEL_PATTERNS if pat.search(p.title)), "unknown")
        if profile.screening_rules and (
            level == "intern" or re.search(r"\b(?:sales|pre-?sales)\b", p.title, re.I)
        ):
            relevance = "none"  # Conservative fallback; AI retries with full eligibility rules.
        elif profile.review_examples and re.search(
            r"\b(?:plc|controls?|robot(?:ics)?|scada|industrial automation)\b", p.title, re.I
        ):
            relevance = "industrial"
        elif excluded.search(p.title):
            relevance = "none"
        elif core.search(p.title):
            relevance = "core"
        elif adjacent.search(p.title):
            relevance = "adjacent"
        else:
            relevance = "none"
        out[p.key] = {
            "relevance": relevance,
            "level": level,
            "reason": "keyword match" if relevance != "none" else "no keyword match",
            "model": KEYWORD_MODEL,
            "classified_at": now,
        }
    return out


def needs_classification(posting: Posting, rules: str) -> bool:
    c = posting.classification
    if posting.status != "active":
        return False
    return c is None or c.get("model") == KEYWORD_MODEL or c.get("rules") != rules


MATCH_GROUPS = {"core": "exact", "adjacent": "near", "maybe": "maybe", "industrial": "industrial"}


def match_group(classification: dict | None, profile: Profile) -> str | None:
    """A match/review group for postings worth showing; None for excluded jobs."""
    if not classification:
        return None
    group = MATCH_GROUPS.get(classification["relevance"])
    if group is None or (profile.mode == "strict" and group not in ("exact", "industrial")):
        return None
    # An unknown level isn't evidence of a mismatch, so keep it.
    level = classification["level"]
    if level != "unknown" and level not in profile.levels:
        return None
    return group


def posting_group(p: Posting, profile: Profile, scope: locations.Scope | None = None) -> str | None:
    """Like match_group, but extended-area jobs only count as exact, and duplicates never do."""
    if p.duplicate_of:
        return None  # shown under the company's own listing instead
    area = scope.area(p) if scope else p.area
    if area is None:
        return None
    group = match_group(p.classification, profile)
    if area == "extended" and group != "exact":
        return None
    return group
