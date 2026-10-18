"""Durable Jev shadow reviews; the existing evaluator remains authoritative."""

import fcntl
import hashlib
import json
import logging
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import httpx

from jobscraper.classification import classify, jev
from jobscraper.config import Config
from jobscraper.storage import jsonfile
from jobscraper.storage.state import Posting
from jobscraper.timezone import EASTERN

log = logging.getLogger(__name__)
DEFAULT_DIR = Path(__file__).resolve().parents[3] / "docs" / "evaluator-comparison"
LABELS = {
    "core": "Exact",
    "adjacent": "Close",
    "maybe": "Need more details",
    "none": "Not a fit",
    "industrial": "Industrial / controls (manual review)",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _text(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def write_report(directory: Path, records: dict) -> None:
    rows = list(records.values())
    done = [r for r in rows if r.get("response")]
    agree = sum(
        r["baseline"]["relevance"] == r["response"]["answers"]["relevance"]["choice"] for r in done
    )
    pending = len(rows) - len(done)
    lines = [
        "# Current evaluator and Jev — ongoing shadow reviews",
        "",
        "Jev evaluates relevance only. The current evaluator still controls the site, "
        "salary eligibility, and notifications. Neither model's answer is ground truth.",
        "",
        f"Recorded assessments: **{len(rows)}** · Compared: **{len(done)}** · "
        f"Awaiting Jev: **{pending}** · Exact label agreement: **{agree}/{len(done)}**.",
        "",
        "This file accumulates new or repeated AI assessments from normal crawls, including "
        "rejected jobs. It does not re-review unchanged postings or import the earlier backfill. "
        "Retries use the original saved inputs. A changed assessment remains a separate record. "
        "Timestamps below use Eastern US time; JSON stores UTC.",
        "",
        "Full inputs, baseline explanations/seniority/scores, Jev probabilities, model versions, "
        "token usage, and retry status are in `records.json`. Review this alongside your "
        "personal ratings before deciding which evaluator fits better.",
        "",
    ]
    versions = Counter(r.get("policy_version", "legacy-v1") for r in rows)
    if versions:
        lines += ["## Rules cohorts", ""]
        for version, count in sorted(versions.items()):
            lines.append(f"- `{_text(version)}`: {count} assessments")
        lines += [
            "",
            "Compare accuracy within the same rules cohort; older results are preserved.",
            "",
        ]
    if not rows:
        lines += [
            "No new AI assessments recorded yet. Collection starts with the next normal crawl.",
            "",
        ]
    for row in sorted(rows, key=lambda r: (r["recorded_at"], r["id"]), reverse=True):
        job = row["request"]["state"]["job"]
        baseline = row["baseline"]
        response = row.get("response")
        stamp = datetime.fromisoformat(row["recorded_at"]).astimezone(EASTERN)
        lines += [
            f"## {_text(job['title'])}",
            "",
            f"{stamp:%b %d, %Y %I:%M %p %Z} · {_text(job['company'])} · {_text(job['location'])}",
            "",
            f"Key: `{_text(row['key'])}` · Assessment: `{row['id'][:12]}`",
            f"Rules version: `{_text(row.get('policy_version', 'legacy-v1'))}` · "
            f"Baseline fingerprint: `{_text(baseline.get('rules', 'unknown'))}`",
            "",
            f"Source excerpt: {_text(job['summary']) or '(No saved summary)'}",
            "",
            f"**Current evaluator: {LABELS[baseline['relevance']]}** "
            f"({baseline.get('model', 'unknown')}; level: {baseline.get('level', 'unknown')}; "
            f"score: {baseline.get('score', '—')}/10)",
            "",
            f"Reason: {_text(baseline.get('reason', ''))}",
            "",
        ]
        if response:
            answer = response["answers"]["relevance"]
            match = "Agree" if baseline["relevance"] == answer["choice"] else "DISAGREE"
            lines += [
                f"**Jev: {LABELS[answer['choice']]}** ({response['model']}; "
                f"confidence: {answer['confidence']:.3f}) · **{match}**",
                "",
            ]
        else:
            lines += [
                f"**Jev: pending** · {_text(row.get('last_error', 'Waiting for review'))}",
                "",
            ]
    temporary = directory / "README.tmp"
    temporary.write_text("\n".join(lines), encoding="utf-8")
    temporary.replace(directory / "README.md")


def collect(
    postings: list[Posting], cfg: Config, directory: Path, client: httpx.Client | None
) -> None:
    """Persist input snapshots first; retry unfinished requests without altering job state."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "shadow.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = directory / "records.json"
        records = jsonfile.read(path)
        for p in postings:
            baseline = p.classification
            if not baseline or baseline.get("model") in (None, classify.KEYWORD_MODEL):
                continue
            request = jev.request_body(p, cfg)
            identity = {"key": p.key, "baseline": baseline, "request": request}
            signature = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            if signature not in records:
                records[signature] = {
                    "id": signature,
                    "key": p.key,
                    "url": p.url,
                    "status": p.status,
                    "recorded_at": _now(),
                    "baseline": baseline.copy(),
                    "request": request,
                    "attempts": 0,
                    "policy_version": baseline.get("policy_version", cfg.profile.rules_version),
                }
        jsonfile.write(path, records)
        write_report(directory, records)
        if client is None:
            if any(not r.get("response") for r in records.values()):
                log.warning("Jev shadow reviews pending: JEV_API_KEY is not set")
            return
        for row in records.values():
            if row.get("response"):
                continue
            row["attempts"] += 1
            row["last_attempt_at"] = _now()
            try:
                row["response"] = jev.evaluate(client, row["request"])
                row["evaluated_at"] = _now()
                row.pop("last_error", None)
            except Exception as exc:
                # Avoid writing arbitrary provider exception text or credentials to docs.
                row["last_error"] = f"{type(exc).__name__}; will retry on the next normal crawl"
                jsonfile.write(path, records)
                write_report(directory, records)
                log.warning("Jev shadow review failed; saved inputs will retry next crawl")
                break  # Don't send every queued job to an unavailable provider.
            jsonfile.write(path, records)
        write_report(directory, records)


def run(postings: list[Posting], cfg: Config, api_key: str) -> None:
    """Optional collection must never turn a completed production crawl into a failure."""
    try:
        if api_key.strip():
            with httpx.Client(
                headers={"Authorization": f"Bearer {api_key.strip()}"}, timeout=30
            ) as client:
                collect(postings, cfg, DEFAULT_DIR, client)
        else:
            collect(postings, cfg, DEFAULT_DIR, None)
    except Exception as exc:
        log.warning("Could not update Jev shadow reviews (%s)", type(exc).__name__)
