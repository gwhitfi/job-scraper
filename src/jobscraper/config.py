import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from jobscraper.geography import distance, locations
from jobscraper.models import Company

DEFAULT_PATH = Path("config.toml")
DEFAULT_MODEL = "claude-sonnet-5-5"
LEVELS = ("intern", "entry", "mid", "senior", "lead")
MODES = ("strict", "adjacent")


@dataclass(frozen=True)
class Profile:
    target_role: str
    mode: str = "adjacent"
    core_examples: tuple[str, ...] = ()
    adjacent_examples: tuple[str, ...] = ()
    exclude_examples: tuple[str, ...] = ()
    levels: tuple[str, ...] = ("intern", "entry", "mid")
    keywords: tuple[str, ...] = ()
    screening_rules: str = ""
    review_examples: tuple[str, ...] = ()
    rules_version: str = "legacy-v1"


@dataclass(frozen=True)
class Notifications:
    email: str


@dataclass(frozen=True)
class Web:
    url: str = ""  # where I open the page; linked from alert emails
    host: str = "127.0.0.1"
    port: int = 9003
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

    def health_names(self) -> dict[str, str]:
        """Company names for health and status messages; same-named entries get their id."""
        counts: dict[str, int] = {}
        for c in self.companies:
            counts[c.name] = counts.get(c.name, 0) + 1
        return {
            c.id: c.name if counts[c.name] == 1 else f"{c.name} ({c.id})" for c in self.companies
        }

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
    profile = Profile(**_tuples(raw["profile"]))
    bad_levels = [lvl for lvl in profile.levels if lvl not in LEVELS]
    if bad_levels:
        raise ValueError(f"Unknown levels {bad_levels} in [profile]; allowed: {list(LEVELS)}")
    if profile.mode not in MODES:
        raise ValueError(f"Unknown mode {profile.mode!r} in [profile]; allowed: {list(MODES)}")
    web = Web(**raw.get("web", {}))
    distance.home_coords(web.home)  # an unknown town would otherwise crash the page
    return Config(
        profile=profile,
        notifications=Notifications(**raw["notifications"]),
        companies=companies,
        regions=regions,
        web=web,
        extended_locations=extended,
        **({"classifier_model": raw["classifier"]["model"]} if "classifier" in raw else {}),
    )
