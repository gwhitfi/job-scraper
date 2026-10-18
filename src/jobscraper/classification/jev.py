"""Independent relevance comparisons; never writes production classifications."""

import fcntl
import hashlib
import json
import math
import time
from collections import Counter
from datetime import UTC, datetime
from itertools import zip_longest
from pathlib import Path

import httpx

from jobscraper.classification import classify, policy
from jobscraper.config import Config
from jobscraper.storage import jsonfile
from jobscraper.storage.state import Posting

MODEL = "jev-1.13.0"
ENDPOINT = "https://api.typesafe.ai/v1/systemone"
CRITERIA = policy.CRITERIA
INSTRUCTIONS = policy.GUIDANCE


def request_body(posting: Posting, cfg: Config, model: str = MODEL) -> dict:
    companies = {c.id: c for c in cfg.companies}
    return {
        "model": model,
        "state": {
            "profile": {name: getattr(cfg.profile, name) for name in classify.PROMPT_FIELDS},
            "job": {
                "title": posting.title,
                "company": classify.company_label(posting, companies),
                "location": posting.location,
                "area": posting.area,
                "category": posting.category,
                "summary": posting.summary[: classify.SUMMARY_CHARS],
            },
        },
        "questions": {
            "relevance": {
                "type": "choice",
                "instructions": INSTRUCTIONS,
                "criteria": CRITERIA,
            }
        },
    }


def select_postings(
    postings: dict[str, Posting], limit: int, include_removed: bool
) -> list[Posting]:
    """Round-robin the saved labels so obvious rejections don't dominate small samples."""
    groups = {label: [] for label in CRITERIA}
    for p in sorted(postings.values(), key=lambda p: p.key):
        c = p.classification or {}
        if (
            (include_removed or p.status == "active")
            and not p.duplicate_of
            and c.get("model") not in (None, classify.KEYWORD_MODEL)
            and c.get("relevance") in groups
        ):
            groups[c["relevance"]].append(p)
    selected = [p for row in zip_longest(*groups.values()) for p in row if p is not None]
    return selected[:limit] if limit else selected


def validate_response(
    data: dict, criteria: dict | None = None, question: str = "relevance"
) -> dict:
    try:
        answer = data["answers"][question]
        probs = answer["probabilities"]
        numbers = [answer["confidence"], *probs.values()]
        if (
            answer["type"] != "choice"
            or answer["choice"] not in probs
            or (
                set(probs) != set(criteria)
                if criteria
                else set(probs) not in (set(CRITERIA), set(CRITERIA) - {"industrial"})
            )
            or any(
                type(n) not in (int, float) or not math.isfinite(n) or not 0 <= n <= 1
                for n in numbers
            )
            or not math.isclose(sum(probs.values()), 1, abs_tol=0.01)
            or not isinstance(data["model"], str)
            or not data["model"]
            or any(
                type(data["usage"][k]) is not int or data["usage"][k] < 0
                for k in ("input_tokens", "output_tokens")
            )
        ):
            raise ValueError
    except (KeyError, TypeError, AttributeError, ValueError):
        raise ValueError(f"Jev returned an invalid {question} response") from None
    return data


def evaluate(client: httpx.Client, body: dict) -> dict:
    for attempt in range(3):
        try:
            response = client.post(ENDPOINT, json=body)
        except httpx.RequestError:
            raise RuntimeError("Jev connection failed; rerun to resume saved results") from None
        if response.status_code in (429, 529) and attempt < 2:
            time.sleep(2 ** (attempt + 1))
            continue
        if response.is_error:
            # Never echo response bodies or request headers that could contain credentials.
            raise RuntimeError(f"Jev API returned HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise RuntimeError("Jev returned an invalid response") from None
        try:
            for name, question in (body.get("questions") or {"relevance": {}}).items():
                validate_response(data, question.get("criteria"), name)
            return data
        except ValueError as exc:
            raise RuntimeError(str(exc)) from None
    raise RuntimeError("Jev retry limit reached")


def compare(
    postings: list[Posting],
    cfg: Config,
    directory: Path,
    client: httpx.Client,
    model: str = MODEL,
) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "compare.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another Jev comparison is already running") from None
        try:
            return _compare(postings, cfg, directory, client, model)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _compare(
    postings: list[Posting], cfg: Config, directory: Path, client: httpx.Client, model: str
) -> dict:
    cache_dir = directory / "cache"
    rows = []
    calls = 0
    input_tokens = 0
    output_tokens = 0
    current_rules = classify.rules_fingerprint(cfg.profile, cfg.classifier_model)
    for p in postings:
        body = request_body(p, cfg, model)
        signature = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        cache_path = cache_dir / f"{signature}.json"
        cached = jsonfile.read(cache_path)
        if cached:
            response = validate_response(cached["response"])
        else:
            response = evaluate(client, body)
            cached = {
                "request": body,
                "response": response,
                "evaluated_at": datetime.now(UTC).isoformat(),
            }
            jsonfile.write(cache_path, cached)
            calls += 1
            input_tokens += response["usage"]["input_tokens"]
            output_tokens += response["usage"]["output_tokens"]
        baseline = p.classification.copy()
        answer = response["answers"]["relevance"]
        rows.append(
            {
                "key": p.key,
                "title": p.title,
                "url": p.url,
                "status": p.status,
                "baseline": baseline,
                "baseline_rules_current": baseline.get("rules") == current_rules,
                "jev": answer,
                "jev_model": response["model"],
                "request_signature": signature,
                "evaluated_at": cached["evaluated_at"],
                "agrees": baseline["relevance"] == answer["choice"],
            }
        )
    matrix = Counter((r["baseline"]["relevance"], r["jev"]["choice"]) for r in rows)
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "requested_model": model,
        "compared": len(rows),
        "agreed": sum(r["agrees"] for r in rows),
        "new_calls": calls,
        "input_tokens_new_calls": input_tokens,
        "output_tokens_new_calls": output_tokens,
        "baseline_rules_current": sum(r["baseline_rules_current"] for r in rows),
        "potential_missed_matches": sum(
            r["baseline"]["relevance"] in ("core", "adjacent") and r["jev"]["choice"] == "none"
            for r in rows
        ),
        "matrix": {a: {b: matrix[a, b] for b in CRITERIA} for a in CRITERIA},
        "caveat": (
            "Agreement is not accuracy. Inputs reflect current saved details, "
            "not historical prompts. Small samples are balanced by baseline label, "
            "not population-representative."
        ),
        "results": rows,
    }
    jsonfile.write(directory / "report.json", report)
    lines = [
        "# Jev relevance comparison",
        "",
        report["caveat"],
        "",
        f"Compared: {len(rows)}; agreed: {report['agreed']}; new calls: {calls}.",
        f"New input tokens: {input_tokens}; new output tokens: {output_tokens}.",
        "",
        "## Disagreements",
        "",
    ]
    for r in rows:
        if not r["agrees"]:
            title = r["title"].replace("\n", " ").replace("\r", " ")
            lines.append(
                f"- {r['key']} — {title}: saved **{r['baseline']['relevance']}**, "
                f"Jev **{r['jev']['choice']}** (confidence {r['jev']['confidence']:.3f})"
            )
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
