import pytest

from jobscraper.geography import distance

HOME = distance.home_coords("Anderson, SC")


@pytest.mark.parametrize(
    ("location", "low", "high"),
    [
        ("ANDERSON, SC, United States of America", 0, 1),
        ("Pendleton, SC, US, 29670", 8, 15),
        ("Greenville, SC 29601", 22, 32),
        ("Spartanburg County, SC", 40, 55),
        ("Athens, GA", 50, 65),
        ("Charlotte, NC", 105, 120),
        ("Townville, SC", 10, 20),  # unincorporated; from the hand-added list
    ],
)
def test_known_distances(location, low, high):
    assert low <= distance.miles_from(HOME, location) <= high


def test_nearest_of_several_places():
    assert distance.miles_from(HOME, "Charlotte, NC; Greenville, SC") < 35


@pytest.mark.parametrize("location", ["Remote", "", "SC", "Austin, TX"])
def test_unknown_or_remote(location):
    assert distance.miles_from(HOME, location) is None


def test_unknown_home_rejected():
    with pytest.raises(ValueError):
        distance.home_coords("Atlantis, SC")


def test_every_region_town_has_coordinates():
    from pathlib import Path

    from jobscraper import config

    cfg = config.load(Path(__file__).parent.parent / "config.toml")
    missing = [c for cities in cfg.regions.values() for c in cities if not distance.coords(c)]
    assert missing == []
