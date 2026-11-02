from pathlib import Path

import pytest

from jobscraper import config
from jobscraper.geography import locations
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
        ("Greenville, SC 29601, USA", True),
        ("Columbia, SC", False),
        ("Charlotte, NC; Greenville, SC", True),  # any listed place counts
        ("Charlotte, NC; Remote", False),
        ("Remote", False),
        ("", False),  # no evidence that this job is in scope
        ("SC-Columbia", False),
        ("Greenville, South Carolina, United States", True),
        ("101 North Tryon Street, Charlotte, North Carolina", False),
        ("123 Main Street, New York, New York", False),
        ("NY; NY", False),  # state only, outside the region
        ("SC", False),  # state only; can't tell which city
    ],
)
def test_region_filter(location, kept):
    f = locations.build_filter(("upstate_sc",), REGIONS)
    assert locations.matches(raw(location), f) is kept


def test_remote_in_title_does_not_override_location():
    f = locations.build_filter(("upstate_sc",), REGIONS)
    assert not locations.matches(raw("Atlanta, GA", title="Developer (Remote)"), f)


def test_explicit_foreign_country_is_not_unknown_or_us_remote():
    from dataclasses import replace

    f = locations.build_filter(("upstate_sc",), REGIONS)
    foreign = replace(raw("Lisboa, Lisbon", "Developer (Remote)"), countries=("Portugal",))
    assert not locations.matches(foreign, f)
    assert not locations.matches(replace(foreign, countries=("Portugal", "US")), f)
    assert not locations.matches(raw(""), f)


@pytest.mark.parametrize(
    "label", ["Non-remote", "Not remote", "Remote or Hybrid", "Remote/Hybrid", "Hybrid or Remote"]
)
def test_ambiguous_or_negated_remote_is_not_unrestricted(label):
    assert not locations.is_remote(raw("New York, NY; " + label))


def test_full_state_normalization_preserves_same_named_city():
    assert locations.places("New York, New York") == [("new york", "NY")]
    assert locations.places("Washington, District of Columbia") == [("washington", "DC")]


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


@pytest.mark.parametrize(
    "location",
    [
        "London, United Kingdom",
        "Toronto, ON",
        "Remote - Canada",
        "Remote - Poland",
        "Lisboa, Lisbon",
        "3 Locations",
        "Singapore",
        "US; Remote; Canada",
        "Florianopolis, SC, Brazil",
        "Greenville, SC, Canada",
    ],
)
def test_unconfirmed_and_foreign_locations_fail_closed(location):
    f = locations.build_filter(("SC",), {})
    assert not locations.matches(raw(location), f)
    assert not locations.matches(raw(location, "Remote Developer"), f)


@pytest.mark.parametrize(
    "location,countries,expected",
    [
        ("Remote", (), False),
        ("Remote", ("US",), True),
        ("Remote", ("US", "Canada"), False),
        ("Remote - United States", (), True),
        ("US Remote", (), True),
        ("Remote - United States", ("Canada",), False),
        ("Toronto, ON; Remote", ("US",), False),
        ("New York, NY; Remote", ("US",), False),
        ("Greenville, SC; Remote", (), True),
    ],
)
def test_remote_requires_confirmed_eligible_location(location, countries, expected):
    from dataclasses import replace

    job = replace(raw(location), countries=countries)
    assert locations.matches(job, locations.build_filter(("SC",), {})) is expected


def test_foreign_state_abbreviation_cannot_match_us_scope():
    f = locations.build_filter(("SC",), {})
    assert not locations.matches(raw("Florianopolis, SC, Brazil"), f)
    assert locations.matches(raw("Florianopolis, SC, Brazil; Greenville, SC, US"), f)


def test_icims_country_state_city_labels():
    from jobscraper.geography.locations import places

    assert places("US-SC-Spartanburg") == [("spartanburg", "SC")]
    assert places("US-NC-Winston-Salem") == [("winston-salem", "NC")]
    assert places("CA-ON-Toronto") == []
