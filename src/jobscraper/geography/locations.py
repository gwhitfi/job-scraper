from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jobscraper.config import Config
from jobscraper.models import RawPosting
from jobscraper.storage.state import Posting

# "Pendleton, SC, US, 29670", "ANDERSON, SC, United States of America", "Greenville, SC"
_CITY_STATE = re.compile(r"([A-Za-z][A-Za-z .'-]*?)\s*,\s*([A-Z]{2})\b")
_STATE = re.compile(r"^[A-Z]{2}$")
# For sites that spell states out ("South Carolina").
STATE_CODES = {
    "Alabama": "AL",
    "Alaska": "AK",
    "Arizona": "AZ",
    "Arkansas": "AR",
    "California": "CA",
    "Colorado": "CO",
    "Connecticut": "CT",
    "Delaware": "DE",
    "District of Columbia": "DC",
    "Florida": "FL",
    "Georgia": "GA",
    "Hawaii": "HI",
    "Idaho": "ID",
    "Illinois": "IL",
    "Indiana": "IN",
    "Iowa": "IA",
    "Kansas": "KS",
    "Kentucky": "KY",
    "Louisiana": "LA",
    "Maine": "ME",
    "Maryland": "MD",
    "Massachusetts": "MA",
    "Michigan": "MI",
    "Minnesota": "MN",
    "Mississippi": "MS",
    "Missouri": "MO",
    "Montana": "MT",
    "Nebraska": "NE",
    "Nevada": "NV",
    "New Hampshire": "NH",
    "New Jersey": "NJ",
    "New Mexico": "NM",
    "New York": "NY",
    "North Carolina": "NC",
    "North Dakota": "ND",
    "Ohio": "OH",
    "Oklahoma": "OK",
    "Oregon": "OR",
    "Pennsylvania": "PA",
    "Rhode Island": "RI",
    "South Carolina": "SC",
    "South Dakota": "SD",
    "Tennessee": "TN",
    "Texas": "TX",
    "Utah": "UT",
    "Vermont": "VT",
    "Virginia": "VA",
    "Washington": "WA",
    "West Virginia": "WV",
    "Wisconsin": "WI",
    "Wyoming": "WY",
}


@dataclass(frozen=True)
class LocationFilter:
    states: frozenset[str]  # whole states, e.g. "SC"
    cities: frozenset[tuple[str, str]]  # (lowercase city, state)
    remote: bool = True

    @property
    def watched_states(self) -> frozenset[str]:
        return self.states | {state for _, state in self.cities}


def build_filter(
    entries: tuple[str, ...], regions: dict[str, tuple[str, ...]], remote: bool = True
) -> LocationFilter:
    """Entries are state codes ("SC"), "City, ST", or region names from config."""
    states, cities = set(), set()
    for entry in entries:
        if _STATE.match(entry) and entry in STATE_CODES.values():
            states.add(entry)
        elif entry in regions:
            cities |= {_city_key(c) for c in regions[entry]}
        elif "," in entry:
            cities.add(_city_key(entry))
        else:
            raise ValueError(f"Unknown location {entry!r}: not a state code, 'City, ST', or region")
    return LocationFilter(frozenset(states), frozenset(cities), remote)


def _city_key(entry: str) -> tuple[str, str]:
    m = _CITY_STATE.fullmatch(entry.strip())
    if not m or m.group(2) not in STATE_CODES.values():
        raise ValueError(f"Expected 'City, ST', got {entry!r}")
    return m.group(1).strip().lower(), m.group(2)


def places(location: str) -> list[tuple[str, str]]:
    """(lowercase city, state) pairs in a location string; city is "" for a bare state code."""
    found = []
    for part in location.split(";"):
        part = part.strip()
        # iCIMS uses country-state-city labels; CA is Canada here, not California.
        country_label = re.fullmatch(r"([A-Z]{2})-([A-Z]{2})-(.+)", part)
        if country_label:
            country, state, city = country_label.groups()
            if country != "US":
                continue
            part = f"{city}, {state}"
        # Full state names occur in Workday addresses; normalize only whole words.
        for name, code in STATE_CODES.items():
            part = re.sub(r"(?<=,)\s*" + re.escape(name) + r"\b", " " + code, part, flags=re.I)
            part = re.sub(
                r"^" + re.escape(name) + r"(?=\s*(?:$|,\s*(?:US|USA|United States)\b))",
                code,
                part,
                flags=re.I,
            )
        part = re.sub(r"^([A-Z]{2})\s*[-–]\s*(.+)$", r"\2, \1", part)
        if _STATE.match(part):
            found.append(("", part))
        else:
            for match in _CITY_STATE.finditer(part):
                city, state = match.groups()
                # Foreign province abbreviations must not become US states.
                suffix = part[match.end() :].strip(" ,")
                if state in STATE_CODES.values() and re.fullmatch(
                    r"(?:(?:US|USA|United States(?: of America)?)(?:,?\s*)?)?"
                    r"(?:\d{5}(?:-\d{4})?(?:,?\s*(?:US|USA|United States(?: of America)?))?)?",
                    suffix,
                    re.I,
                ):
                    found.append((city.strip().lower(), state))
    return found


def is_remote(p: RawPosting | Posting) -> bool:
    value = p.location + "; " + p.title
    value = re.sub(
        r"\b(?:not|non)[ -]remote\b|\bremote\s*(?:or|/)\s*hybrid\b|\bhybrid\s*(?:or|/)\s*remote\b",
        "",
        value,
        flags=re.I,
    )
    return bool(re.search(r"\bremote\b", value, re.I))


def us_country(country: str) -> bool:
    return country.strip().casefold() in {"us", "usa", "united states", "united states of america"}


def matches(p: RawPosting | Posting, f: LocationFilter) -> bool:
    if p.countries and not any(us_country(c) for c in p.countries):
        return False  # A known foreign location is not an unknown US location, even if remote.
    found = places(p.location)
    in_area = any(state in f.states or (city, state) in f.cities for city, state in found)
    if in_area:
        return True
    if not f.remote or not is_remote(p):
        return False
    # Only explicitly nationwide US remote work can bypass the local area.
    # City/state restrictions and unknown labels must not become "anywhere".
    label = re.sub(r"\bremote\b", "", p.location, flags=re.I).strip(" ,;-()")
    return us_country(label) or (
        not label and bool(p.countries) and all(us_country(c) for c in p.countries)
    )


def apply(
    postings: list[RawPosting], f: LocationFilter
) -> tuple[list[RawPosting], list[RawPosting]]:
    """Split into (kept, dropped). Unconfirmed locations are dropped."""
    kept, dropped = [], []
    for p in postings:
        (kept if matches(p, f) else dropped).append(p)
    return kept, dropped


def possible_gaps(dropped: list[RawPosting], f: LocationFilter) -> list[RawPosting]:
    """Dropped jobs in a watched state; a city missing from a region list shows up here."""
    return [p for p in dropped if any(state in f.watched_states for _, state in places(p.location))]


@dataclass(frozen=True)
class Scope:
    primary: LocationFilter
    extended: LocationFilter | None = None

    def area(self, posting: RawPosting | Posting) -> str | None:
        if matches(posting, self.primary):
            return "primary"
        if self.extended and matches(posting, self.extended):
            return "extended"
        return None


def scopes(cfg: Config) -> dict[str, Scope]:
    extended = (
        build_filter(cfg.extended_locations, cfg.regions, remote=False)
        if cfg.extended_locations
        else None
    )
    return {
        c.id: Scope(build_filter(c.locations, cfg.regions, c.remote), extended)
        for c in cfg.companies
        if c.locations
    }
