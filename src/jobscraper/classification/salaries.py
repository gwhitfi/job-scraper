"""Estimated base pay, including a resumable salary-only backfill."""

from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Self

from pydantic import BaseModel, model_validator

if TYPE_CHECKING:
    from jobscraper.storage.state import Posting


class Salary(BaseModel):
    minimum: float
    maximum: float
    period: Literal["year", "hour"]
    currency: Literal["USD"]
    confidence: Literal["low", "medium"]

    @model_validator(mode="after")
    def valid_range(self) -> Self:
        ceiling = 10000 if self.period == "hour" else 10000000
        if not 0 < self.minimum <= self.maximum <= ceiling:
            raise ValueError("Salary range must be positive, ordered, and within plausible bounds")
        return self


INSTRUCTIONS = """
First finish the relevance and seniority decision. If the job fails the supplied pay
eligibility rules, return salary=null immediately; do not work out a salary for it.
For qualifying jobs only, estimate broad base pay for title, seniority, and US location.
Return salary with minimum, maximum, period (year or hour), currency USD, and confidence
(low or medium), or null if there is insufficient information. Use hourly pay for hourly
roles/internships when appropriate. Exclude bonuses, equity, benefits, and commission.
These are approximate model estimates, not researched or verified employer offers.
Do not imply access to current salary surveys. Use low confidence for ambiguous titles,
remote roles without a clear pay market, or multiple locations with different pay markets.
Do not infer a salary for placeholders, general applications, or non-US-only locations.
"""


def eligible(posting: "Posting", profile, scope=None, marks: dict | None = None) -> bool:
    from jobscraper.classification import classify

    marks = marks or {}
    if posting.duplicate_of or marks.get("hidden"):
        return False
    area = scope.area(posting) if scope else posting.area
    if area is None:
        return False
    if marks.get("group") in ("exact", "near", "maybe"):
        return True
    return classify.posting_group(posting, profile, scope) is not None


def serialize(value: Salary | None, model: str) -> dict:
    return {
        "estimate": value.model_dump() if value else None,
        "model": model,
        "estimated_at": datetime.now(UTC).isoformat(),
    }


def annual_floor(posting: "Posting") -> float | None:
    saved = (posting.classification or {}).get("salary") or posting.salary
    if not saved or not saved.get("estimate"):
        return None
    try:
        value = Salary.model_validate(saved["estimate"])
    except (ValueError, TypeError):
        return None
    return value.minimum * (2080 if value.period == "hour" else 1)


def card(posting: "Posting") -> str:
    saved = (posting.classification or {}).get("salary") or posting.salary
    if not saved:
        return ""
    if saved.get("estimate") is None:
        return '<p class="salary muted">AI-estimated pay: insufficient information</p>'
    try:
        value = Salary.model_validate(saved["estimate"])
    except (ValueError, TypeError):
        return ""

    def money(amount: float) -> str:
        return f"${amount:,.0f}" if amount.is_integer() else f"${amount:,.2f}"

    return (
        '<p class="salary" title="Approximate base pay based on title and location; '
        'not a verified employer salary or live market research.">'
        f"<span>AI-estimated pay</span> <strong>{money(value.minimum)}–{money(value.maximum)}"
        f"/{value.period}</strong> USD · {escape(value.confidence)} confidence</p>"
    )


class SalaryResult(BaseModel):
    id: str
    salary: Salary | None


class SalaryResults(BaseModel):
    results: list[SalaryResult]


def backfill(data_dir: Path) -> None:
    """Add salaries only to qualifying jobs, saving every paid batch."""
    import uuid

    import anthropic
    from dotenv import find_dotenv, load_dotenv

    from jobscraper import config
    from jobscraper.classification import classify, usage
    from jobscraper.classification.classification_cache import Checkpoint
    from jobscraper.geography import locations
    from jobscraper.storage import company_store, jsonfile, state
    from jobscraper.workflow import scheduling

    load_dotenv(find_dotenv(usecwd=True))
    cfg = company_store.merge(config.load(data_dir / "config.toml"), data_dir / "companies.json")
    companies = {c.id: c for c in cfg.companies}
    scopes = locations.scopes(cfg)
    marks = jsonfile.read(data_dir / "marks.json")
    started = datetime.now(UTC)
    run_id = "salary-" + uuid.uuid4().hex
    spent = usage.Usage()
    system = (
        "These postings have already passed eligibility. Estimate base pay for each posting. "
        "Return each supplied id exactly once." + INSTRUCTIONS
    )
    with scheduling.run_lock(data_dir):
        scheduling.snapshot(data_dir, "before-salary-backfill")
    cache = Checkpoint(data_dir / "state/salary-cache.json", run_id, cfg.classifier_model, started)
    initial = state.load(data_dir / "state")
    jobs = [
        p
        for p in initial.postings.values()
        if not ((p.classification or {}).get("salary") or p.salary)
        and eligible(p, cfg.profile, scopes.get(p.company_id), marks.get(p.key))
        and (
            p.status == "active"
            or any(marks.get(p.key, {}).get(k) for k in ("starred", "applied", "note"))
        )
    ]
    total = len(jobs)
    print(f"Salary backfill: {total} postings", flush=True)
    status_path = data_dir / "salary-backfill.json"
    completed = 0

    def progress(status: str, error: str | None = None) -> None:
        jsonfile.write(
            status_path,
            dict(
                id=run_id,
                started=started.isoformat(),
                status=status,
                completed=completed,
                total=total,
                error=error,
            ),
        )

    progress("running")
    if not jobs:
        progress("completed")
        return
    try:
        with anthropic.Anthropic() as client:
            for offset in range(0, total, 50):
                batch = jobs[offset : offset + 50]
                signatures = {
                    p.key: cache.signature(
                        classify._prompt([p], cfg.profile, companies), system + cfg.classifier_model
                    )
                    for p in batch
                }
                results = {
                    p.key: cached
                    for p in batch
                    if (cached := cache.get(p.key, signatures[p.key])) is not None
                }
                missing = [p for p in batch if p.key not in results]
                if missing:
                    response = client.messages.parse(
                        model=cfg.classifier_model,
                        max_tokens=12000,
                        system=system,
                        messages=[
                            {
                                "role": "user",
                                "content": classify._prompt(missing, cfg.profile, companies),
                            }
                        ],
                        output_format=SalaryResults,
                    )
                    spent.add(response.usage)
                    # Account for paid requests even when their results are incomplete.
                    cache.save({}, {}, spent)
                    if response.stop_reason != "end_turn" or response.parsed_output is None:
                        raise RuntimeError("Salary response was incomplete; rerun to resume")
                    wanted = {p.key for p in missing}
                    items = response.parsed_output.results
                    if len(items) != len(wanted) or {x.id for x in items} != wanted:
                        raise RuntimeError("Salary response IDs did not match the requested jobs")
                    results.update({x.id: serialize(x.salary, cfg.classifier_model) for x in items})
                    cache.save(results, signatures, spent)
                # Reload under the crawl lock so a concurrent crawl or mark isn't overwritten.
                with scheduling.run_lock(data_dir):
                    current = state.load(data_dir / "state")
                    latest_marks = jsonfile.read(data_dir / "marks.json")
                    for p in batch:
                        latest = current.postings.get(p.key)
                        if (
                            latest
                            and eligible(
                                latest,
                                cfg.profile,
                                scopes.get(latest.company_id),
                                latest_marks.get(latest.key),
                            )
                            and cache.signature(
                                classify._prompt([latest], cfg.profile, companies),
                                system + cfg.classifier_model,
                            )
                            == signatures[p.key]
                        ):
                            latest.salary = results[p.key]
                    state.save(current, data_dir / "state")
                completed += len(batch)
                progress("running")
                print(
                    f"Salary backfill: {completed}/{total}; "
                    f"${spent.cost(cfg.classifier_model):.4f}",
                    flush=True,
                )
        progress("completed")
    except Exception as exc:
        progress("failed", str(exc))
        raise
    finally:
        with scheduling.run_lock(data_dir):
            scheduling.snapshot(data_dir, "after-salary-backfill")


if __name__ == "__main__":
    backfill(Path.home() / ".local/share/jobscraper")
