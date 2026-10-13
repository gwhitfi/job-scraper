import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from jobscraper import locations
from jobscraper.models import Company

DEFAULT_PATH = Path("config.toml")
DEFAULT_MODEL = "claude-sonnet-5-5"


@dataclass(frozen=True)
class Profile:
    target_role: str
    mode: str = "adjacent"
    core_examples: tuple[str, ...] = ()
    adjacent_examples: tuple[str, ...] = ()
    exclude_examples: tuple[str, ...] = ()
    levels: tuple[str, ...] = ("intern", "entry", "mid")
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class Notifications:
    email: str


@dataclass(frozen=True)
class Web:
    url: str = ""  # where I open the page; linked from alert emails
    host: str = "127.0.0.1"
    port: int = 8787
    home: str = "Anderson, SC"  # distances on the page are measured from here


@dataclass(frozen=True)
class Config:
    profile: Profile
    notifications: Notifications
    companies: list[Company] = field(default_factory=list)
    classifier_model: str = DEFAULT_MODEL
    regions: dict[str, tuple[str, ...]] = field(default_factory=dict)
    web: Web = field(default_factory=Web)
    # Wider area for companies that use `locations`; only exact matches from it are shown.
    extended_locations: tuple[str, ...] = ()

    def company(self, company_id: str) -> Company:
        for c in self.companies:
            if c.id == company_id:
                return c
        raise KeyError(f"No company with id {company_id!r} in config")


def _tuples(d: dict) -> dict:
    return {k: tuple(v) if isinstance(v, list) else v for k, v in d.items()}


def load(path: Path = DEFAULT_PATH) -> Config:
    with path.open("rb") as f:
        raw = tomllib.load(f)
    companies = [Company(**_tuples(c)) for c in raw.get("companies", [])]
    ids = [c.id for c in companies]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate company ids in config")
    regions = {name: tuple(cities) for name, cities in raw.get("regions", {}).items()}
    extended = tuple(raw.get("locations", {}).get("extended", ()))
    locations.build_filter(extended, regions)  # fail at load on typos
    for c in companies:
        locations.build_filter(c.locations, regions)
    return Config(
        profile=Profile(**_tuples(raw["profile"])),
        notifications=Notifications(**raw["notifications"]),
        companies=companies,
        regions=regions,
        web=Web(**raw.get("web", {})),
        extended_locations=extended,
        **({"classifier_model": raw["classifier"]["model"]} if "classifier" in raw else {}),
    )
