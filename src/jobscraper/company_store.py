"""Companies added through the page, kept beside my marks and merged into each run."""

from dataclasses import asdict, replace
from datetime import date
from pathlib import Path

from jobscraper import jsonfile
from jobscraper.config import Config
from jobscraper.models import Company


def load(path: Path) -> list[Company]:
    out = []
    for values in jsonfile.read(path).values():
        values = dict(values)
        values["added"] = date.fromisoformat(values["added"])
        for key in ("locations", "aliases"):
            values[key] = tuple(values.get(key, ()))
        out.append(Company(**values))
    return out


def merge(cfg: Config, path: Path) -> Config:
    companies = {c.id: c for c in cfg.companies}
    for company in load(path):
        if company.id in companies:
            raise ValueError(f"Company id {company.id!r} exists in both config and companies.json")
        companies[company.id] = company
    return replace(cfg, companies=list(companies.values()))


def add(path: Path, company: Company) -> None:
    saved = jsonfile.read(path)
    if company.id in saved:
        raise ValueError("This company is already being tracked.")
    saved[company.id] = asdict(company) | {"added": company.added.isoformat()}
    jsonfile.write(path, saved)
