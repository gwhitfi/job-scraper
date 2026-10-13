from pathlib import Path

import pytest

from jobscraper import config, locations
from jobscraper.models import RawPosting

REGIONS = {"upstate_sc": ("Greenville, SC", "Pendleton, SC", "Sandy Springs, SC")}


def raw(location: str, title: str = "Dev") -> RawPosting:
    return RawPosting(source_id=location, title=title, location=location, url="u")


@pytest.mark.parametrize(
    ("location", "kept"),
    [
        ("Pendleton, SC, US, 29670", True),  # Arthrex format
        ("ANDERSON, SC, United States of America", False),  # Michelin format, not in test list
        ("SANDY SPRINGS, SC, United States of America", True),
        ("Greenville, SC", True),
        ("Columbia, SC", False),
        ("Charlotte, NC; Greenville, SC", True),  # any listed place counts
        ("Charlotte, NC; Remote", True),
        ("Remote", True),
        ("", True),  # can't tell, so keep
        ("SC-Columbia", True),  # unparseable, so keep
        ("NY; NY", False),  # state only, outside the region
        ("SC", False),  # state only; can't tell which city
    ],
)
def test_region_filter(location, kept):
    f = locations.build_filter(("upstate_sc",), REGIONS)
    assert locations.matches(raw(location), f) is kept


def test_remote_in_title_kept():
    f = locations.build_filter(("upstate_sc",), REGIONS)
    assert locations.matches(raw("Atlanta, GA", title="Developer (Remote)"), f)


def test_remote_can_be_excluded():
    f = locations.build_filter(("upstate_sc",), REGIONS, remote=False)
    assert not locations.matches(raw("Remote"), f)
    assert not locations.matches(raw("Charlotte, NC; Remote"), f)
    assert locations.matches(raw("Greenville, SC; Remote"), f)  # also lists a local office
    assert locations.matches(raw("Greenville, SC"), f)


def test_state_and_city_entries():
    f = locations.build_filter(("NC", "Columbia, SC"), REGIONS)
    assert locations.matches(raw("Charlotte, NC"), f)
    assert locations.matches(raw("COLUMBIA, SC, US"), f)
    assert not locations.matches(raw("Greenville, SC"), f)


def test_unknown_entry_rejected():
    with pytest.raises(ValueError):
        locations.build_filter(("upstate",), REGIONS)


def test_possible_gaps_only_in_watched_states():
    f = locations.build_filter(("upstate_sc",), REGIONS)
    postings = [raw("Wellford, SC"), raw("Austin, TX"), raw("Greenville, SC")]
    kept, dropped = locations.apply(postings, f)
    assert [p.location for p in kept] == ["Greenville, SC"]
    assert [p.location for p in locations.possible_gaps(dropped, f)] == ["Wellford, SC"]


@pytest.mark.parametrize(
    "location",
    [
        "Pendleton, SC, US, 29670",
        "ANDERSON, SC, United States of America",
        "SANDY SPRINGS, SC, United States of America",
        "Greenville, SC",
        "Spartanburg, SC",
        "Greer, SC",
    ],
)
def test_repo_upstate_region_covers_known_job_cities(location):
    cfg = config.load(Path(__file__).parent.parent / "config.toml")
    f = locations.build_filter(("upstate_sc",), cfg.regions)
    assert locations.matches(raw(location), f)
