import csv
import math
from functools import cache
from importlib.resources import files

from jobscraper import locations

EARTH_RADIUS_MILES = 3958.8


@cache
def _places() -> dict[tuple[str, str], tuple[float, float]]:
    """(lowercase name, state) -> (lat, lon), built by scripts/build_places.py."""
    with files("jobscraper").joinpath("data/places.csv").open(encoding="utf-8") as f:
        return {
            (r["name"].lower(), r["state"]): (float(r["lat"]), float(r["lon"]))
            for r in csv.DictReader(f)
        }


@cache  # the page recomputes distances on every load; locations repeat a lot
def coords(location: str) -> tuple[tuple[float, float], ...]:
    """Coordinates of every known place in a location string (postings can list several)."""
    places = _places()
    return tuple(places[p] for p in locations.places(location) if p in places)


def miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Straight-line (great-circle) distance."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(h))


def home_coords(home: str) -> tuple[float, float]:
    found = coords(home)
    if not found:
        raise ValueError(f"Unknown home location {home!r}; expected a 'City, ST' in SC, NC, or GA")
    return found[0]


def miles_from(home: tuple[float, float], location: str) -> float | None:
    """Distance to the nearest listed place; None for remote or unknown locations."""
    found = coords(location)
    return min(miles(home, c) for c in found) if found else None
