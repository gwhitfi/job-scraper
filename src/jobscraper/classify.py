import hashlib
import json
import logging
import re
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Literal

import anthropic
from pydantic import BaseModel

from jobscraper.config import DEFAULT_MODEL, Profile
from jobscraper.models import Company
from jobscraper.state import Posting
from jobscraper.usage import Usage

log = logging.getLogger(__name__)

BATCH_SIZE = 50  # keeps each reply well under max_tokens
KEYWORD_MODEL = "keywords"  # marks fallback results so they get retried with the LLM

Relevance = Literal["core", "adjacent", "maybe", "none"]
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


class Results(BaseModel):
    results: list[Result]


SYSTEM = """\
You screen job postings for one job seeker. For each posting, judge mainly from the \
title, using the company, category, and summary to tell what kind of work it is:

relevance:
- "core": the target role, or the same job under another name or abbreviation (the \
seeker lists equivalent titles; treat close variants of those the same way)
- "adjacent": a related role the seeker listed as acceptable, or one very similar to those
- "maybe": could plausibly be core or adjacent, but the posting doesn't say enough to tell
- "none": clearly not a fit, including roles the seeker excluded

Adjacent roles must be in the same field as the target role. Sharing a word with it or \
with an acceptable role is not enough: for a software target, a manufacturing "QA \
Engineer" or "Quality Technician" is "none".

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


SUMMARY_CHARS = 300


def company_label(p: Posting, companies: dict[str, Company]) -> str:
    c = companies.get(p.company_id)
    if c is None:
        return p.employer or p.company_id
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
        "",
        "Postings:",
    ]
    for p in postings:
        line = f"- id={p.key} | {p.title} | {company_label(p, companies)} | {p.location}"
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
)


def rules_fingerprint(profile: Profile, model: str) -> str:
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
) -> dict[str, dict]:
    """Classify in batches; postings that can't be classified are left out for a retry."""

    def attempt(batch: list[Posting]) -> dict[str, dict]:
        """If a batch's content breaks the reply, split it to isolate the bad posting."""
        try:
            return classify_batch(batch, profile, companies, client, model, usage)
        except Exception as e:
            if _unavailable(e):
                raise
            if len(batch) == 1:
                log.warning("Couldn't classify %s: %s: %s", batch[0].key, type(e).__name__, e)
                return {}
            log.warning("Batch of %d failed (%s: %s); splitting", len(batch), type(e).__name__, e)
            mid = len(batch) // 2
            return attempt(batch[:mid]) | attempt(batch[mid:])

    results: dict[str, dict] = {}
    for start in range(0, len(postings), BATCH_SIZE):
        try:
            results |= attempt(postings[start : start + BATCH_SIZE])
        except Exception as e:
            if not _unavailable(e):
                raise
            # Claude itself is down or refusing us; more calls this run would fail too.
            log.warning("Claude unavailable, skipping %d postings: %s", len(postings) - start, e)
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
        system=SYSTEM,
        messages=[{"role": "user", "content": _prompt(postings, profile, companies)}],
        output_format=Results,
    )
    if usage is not None and getattr(response, "usage", None):
        usage.add(response.usage)  # billed even if the reply turns out unusable
    if response.stop_reason != "end_turn" or response.parsed_output is None:
        raise RuntimeError(f"Classifier stopped early: {response.stop_reason}")

    now = datetime.now(UTC).isoformat()
    wanted = {p.key for p in postings}
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
            "classified_at": now,
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
    now = datetime.now(UTC).isoformat()
    out = {}
    for p in postings:
        if core.search(p.title):
            relevance = "core"
        elif adjacent.search(p.title):
            relevance = "adjacent"
        else:
            relevance = "none"
        level = next((lvl for lvl, pat in _LEVEL_PATTERNS if pat.search(p.title)), "unknown")
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


MATCH_GROUPS = {"core": "exact", "adjacent": "near", "maybe": "maybe"}


def match_group(classification: dict | None, profile: Profile) -> str | None:
    """The group ("exact", "near", "maybe") for postings worth showing; None for the rest."""
    if not classification:
        return None
    group = MATCH_GROUPS.get(classification["relevance"])
    if group is None or (profile.mode == "strict" and group != "exact"):
        return None
    # An unknown level isn't evidence of a mismatch, so keep it.
    level = classification["level"]
    if level != "unknown" and level not in profile.levels:
        return None
    return group


def posting_group(p: Posting, profile: Profile) -> str | None:
    """Like match_group, but extended-area jobs only count as exact, and duplicates never do."""
    if p.duplicate_of:
        return None  # shown under the company's own listing instead
    group = match_group(p.classification, profile)
    if p.area == "extended" and group != "exact":
        return None
    return group
