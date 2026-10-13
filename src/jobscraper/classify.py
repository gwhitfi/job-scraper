import re
from datetime import UTC, datetime
from typing import Literal

import anthropic
from pydantic import BaseModel

from jobscraper.config import Profile
from jobscraper.state import Posting

DEFAULT_MODEL = "claude-sonnet-5-5"
KEYWORD_MODEL = "keywords"  # marks fallback results so they get retried with the LLM

Relevance = Literal["core", "adjacent", "none"]
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
- "core": the target role itself or a clear variant of it
- "adjacent": a related role the seeker listed as acceptable, or one very similar to those
- "none": anything else, including roles the seeker excluded

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


def _prompt(postings: list[Posting], profile: Profile, company_names: dict[str, str]) -> str:
    lines = [
        f"Target role: {profile.target_role}",
        f"Acceptable adjacent roles: {', '.join(profile.adjacent_examples) or 'none listed'}",
        f"Excluded roles: {', '.join(profile.exclude_examples) or 'none listed'}",
        f"Helpful keywords: {', '.join(profile.keywords) or 'none listed'}",
        "",
        "Postings:",
    ]
    for p in postings:
        company = company_names.get(p.company_id, p.company_id)
        line = f"- id={p.key} | {p.title} | {company} | {p.location}"
        if p.category:
            line += f" | category: {p.category}"
        if p.summary:
            line += f" | summary: {p.summary[:SUMMARY_CHARS]}"
        lines.append(line)
    return "\n".join(lines)


def classify_llm(
    postings: list[Posting],
    profile: Profile,
    company_names: dict[str, str],
    client: anthropic.Anthropic,
    model: str = DEFAULT_MODEL,
) -> dict[str, dict]:
    """Classify all postings in one request. Postings missing from the reply are left out."""
    response = client.messages.parse(
        model=model,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": _prompt(postings, profile, company_names)}],
        output_format=Results,
    )
    if response.stop_reason != "end_turn" or response.parsed_output is None:
        raise RuntimeError(f"Classifier stopped early: {response.stop_reason}")

    now = datetime.now(UTC).isoformat()
    wanted = {p.key for p in postings}
    return {
        r.id: {
            "relevance": r.relevance,
            "level": r.level,
            "reason": r.reason,
            "model": model,
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
    core = [k.lower() for k in (profile.target_role, *profile.keywords)]
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


def needs_classification(posting: Posting) -> bool:
    c = posting.classification
    return posting.status == "active" and (c is None or c.get("model") == KEYWORD_MODEL)


def is_relevant(classification: dict | None, profile: Profile) -> bool:
    if not classification:
        return False
    allowed = {"core"} if profile.mode == "strict" else {"core", "adjacent"}
    if classification["relevance"] not in allowed:
        return False
    # An unknown level isn't evidence of a mismatch, so keep it.
    return classification["level"] == "unknown" or classification["level"] in profile.levels
