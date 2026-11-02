"""Companies added through the page, kept beside my marks and merged into each run."""

from dataclasses import asdict, replace
from datetime import date
from pathlib import Path

from jobscraper.config import Config
from jobscraper.models import Company
from jobscraper.storage import jsonfile


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
    names = jsonfile.read(path.with_name("company-names.json"))
    return replace(
        cfg,
        companies=[
            replace(c, name=names[c.id], aliases=tuple(dict.fromkeys((*c.aliases, c.name))))
            if c.id in names and names[c.id] != c.name
            else c
            for c in companies.values()
        ],
    )


def add(path: Path, company: Company) -> None:
    saved = jsonfile.read(path)
    if company.id in saved:
        raise ValueError("This company is already being tracked.")
    saved[company.id] = asdict(company) | {"added": company.added.isoformat()}
    jsonfile.write(path, saved)


def rename(path: Path, company_id: str, name: str) -> None:
    if not isinstance(name, str):
        raise ValueError("Company name must be text.")
    name = name.strip()
    if not name or len(name) > 200 or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ValueError("Company name must be 1–200 characters without control characters.")
    names_path = path.with_name("company-names.json")
    names = jsonfile.read(names_path)
    names[company_id] = name
    jsonfile.write(names_path, names)
