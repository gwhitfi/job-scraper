import re
from dataclasses import dataclass

from jobscraper.models import RawPosting

# "Pendleton, SC, US, 29670", "ANDERSON, SC, United States of America", "Greenville, SC"
_CITY_STATE = re.compile(r"([A-Za-z][A-Za-z .'-]*?)\s*,\s*([A-Z]{2})\b")
_STATE = re.compile(r"^[A-Z]{2}$")


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
        if _STATE.match(entry):
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
    if not m:
        raise ValueError(f"Expected 'City, ST', got {entry!r}")
    return m.group(1).strip().lower(), m.group(2)


def places(location: str) -> list[tuple[str, str]]:
    """(lowercase city, state) pairs in a location string; city is "" for a bare state code."""
    found = []
    for part in location.split(";"):
        part = part.strip()
        if _STATE.match(part):
            found.append(("", part))
        else:
            found += [(city.strip().lower(), state) for city, state in _CITY_STATE.findall(part)]
    return found


def is_remote(p: RawPosting) -> bool:
    return "remote" in p.location.lower() or "remote" in p.title.lower()


def matches(p: RawPosting, f: LocationFilter) -> bool:
    found = places(p.location)
    in_area = any(state in f.states or (city, state) in f.cities for city, state in found)
    if is_remote(p):
        return f.remote or in_area
    # Unparseable or missing location: keep it rather than drop a job silently.
    return in_area or not found


def apply(
    postings: list[RawPosting], f: LocationFilter
) -> tuple[list[RawPosting], list[RawPosting]]:
    """Split into (kept, dropped). Jobs without a parseable location are kept."""
    kept, dropped = [], []
    for p in postings:
        (kept if matches(p, f) else dropped).append(p)
    return kept, dropped


def possible_gaps(dropped: list[RawPosting], f: LocationFilter) -> list[RawPosting]:
    """Dropped jobs in a watched state; a city missing from a region list shows up here."""
    return [p for p in dropped if any(state in f.watched_states for _, state in places(p.location))]
