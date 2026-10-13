import hashlib
import json
import logging
import re
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Literal

import anthropic
from pydantic import BaseModel

from jobscraper.config import Profile
from jobscraper.models import Company
from jobscraper.state import Posting

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-5-5"
BATCH_SIZE = 50  # keeps each reply well under max_tokens
KEYWORD_MODEL = "keywords"  # marks fallback results so they get retried with the LLM

Relevance = Literal["core", "adjacent", "maybe", "none"]
Level = Literal["intern", "entry", "mid", "senior", "lead", "unknown"]


class Result(BaseModel):
    # Fields are generated in this order, so the reason comes before the verdict.
    id: str
    reason: str
    relevance: Relevance
    level: Level


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

level:
- "intern": internships, co-ops, summer programs
- "entry": junior, associate, "I", new grad, trainee
- "mid": no seniority marker, or "II"
- "senior": senior, sr, "III", staff
- "lead": lead, principal, manager, director, head of
- "unknown": the title gives no usable signal

For each posting, first write a reason (at most 20 words) saying what kind of work the job \
is, then give relevance and level. Return one result per posting, using its id."""


SUMMARY_CHARS = 300


def company_label(p: Posting, companies: dict[str, Company]) -> str:
    c = companies.get(p.company_id)
    if c is None:
        return p.employer or p.company_id
    if p.employer:
        return f"{p.employer} (via {c.name})"
    return f"{c.name} ({c.about})" if c.about else c.name


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


def rules_fingerprint(profile: Profile, model: str) -> str:
    """Changes whenever the profile, prompt, or model changes, so stale results get redone."""
    blob = json.dumps([asdict(profile), SYSTEM, model], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def classify_llm(
    postings: list[Posting],
    profile: Profile,
    companies: dict[str, Company],
    client: anthropic.Anthropic,
    model: str = DEFAULT_MODEL,
) -> dict[str, dict]:
    """Classify in batches. A failed batch is logged and its postings left out for a retry."""
    results: dict[str, dict] = {}
    for start in range(0, len(postings), BATCH_SIZE):
        batch = postings[start : start + BATCH_SIZE]
        try:
            results |= classify_batch(batch, profile, companies, client, model)
        except (anthropic.APIError, RuntimeError) as e:
            log.warning("Classifier batch of %d failed: %s", len(batch), e)
    return results


def classify_batch(
    postings: list[Posting],
    profile: Profile,
    companies: dict[str, Company],
    client: anthropic.Anthropic,
    model: str = DEFAULT_MODEL,
) -> dict[str, dict]:
    """Classify postings in one request. Postings missing from the reply are left out."""
    response = client.messages.parse(
        model=model,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": _prompt(postings, profile, companies)}],
        output_format=Results,
    )
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


def classify_keywords(postings: list[Posting], profile: Profile) -> dict[str, dict]:
    """Rough title-only fallback for when the LLM is unavailable."""
    core = [k.lower() for k in (profile.target_role, *profile.core_examples, *profile.keywords)]
    adjacent = [k.lower() for k in profile.adjacent_examples]
    now = datetime.now(UTC).isoformat()
    out = {}
    for p in postings:
        title = p.title.lower()
        if any(k in title for k in core):
            relevance = "core"
        elif any(k in title for k in adjacent):
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
    """ "exact", "near", or "maybe" for postings worth showing; None for the rest."""
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


def is_relevant(classification: dict | None, profile: Profile) -> bool:
    return match_group(classification, profile) is not None
