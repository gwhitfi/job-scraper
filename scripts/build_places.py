"""Build src/jobscraper/data/places.csv from the Census Gazetteer files.

Download and unzip 2024_Gaz_place_national.zip and 2024_Gaz_counties_national.zip from
https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.html, then:

    uv run python scripts/build_places.py 2024_Gaz_place_national.txt 2024_Gaz_counties_national.txt
"""

import csv
import re
import sys
from pathlib import Path

STATES = {"SC", "NC", "GA"}
OUT = Path(__file__).parent.parent / "src" / "jobscraper" / "data" / "places.csv"
# "Anderson city", "Taylors CDP", "Athens-Clarke County unified government (balance)"
_SUFFIX = re.compile(
    r"\s+(city|town|village|CDP|unified government|consolidated government)(\s+\(balance\))?$"
)


# Unincorporated communities the Gazetteer doesn't list; coordinates are approximate.
EXTRA = {
    ("SC", "Townville"): (34.567, -82.894),
    ("SC", "Moore"): (34.866, -81.993),
    ("NC", "Arden"): (35.466, -82.517),
    ("GA", "Eastanollee"): (34.530, -83.252),
}
# Combined Census areas whose parts appear on their own in job listings.
SPLIT = {"Slater-Marietta": ("Slater", "Marietta")}


def rows(path: str):
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        reader.fieldnames = [n.strip() for n in reader.fieldnames]
        for r in reader:
            if r["USPS"] in STATES:
                yield r


def main(place_file: str, county_file: str) -> None:
    places: dict[tuple[str, str], tuple[float, float]] = {}

    def add(state: str, name: str, lat: str, lon: str) -> None:
        places.setdefault((state, name), (round(float(lat), 4), round(float(lon.strip()), 4)))

    for r in rows(place_file):
        name = _SUFFIX.sub("", r["NAME"])
        add(r["USPS"], name, r["INTPTLAT"], r["INTPTLONG"])
        if "-" in name and "County" in name:  # "Athens-Clarke County" is usually just "Athens"
            add(r["USPS"], name.split("-")[0], r["INTPTLAT"], r["INTPTLONG"])
        for part in SPLIT.get(name, ()):
            add(r["USPS"], part, r["INTPTLAT"], r["INTPTLONG"])
    for r in rows(county_file):
        add(r["USPS"], r["NAME"], r["INTPTLAT"], r["INTPTLONG"])

    for key, coords in EXTRA.items():
        places.setdefault(key, coords)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["state", "name", "lat", "lon"])
        for (state, name), (lat, lon) in sorted(places.items()):
            w.writerow([state, name, lat, lon])
    print(f"wrote {len(places)} places to {OUT}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
