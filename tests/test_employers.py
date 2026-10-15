from datetime import UTC, date, datetime

import pytest

from jobscraper.models import Company
from jobscraper.storage import employers, jsonfile
from jobscraper.storage.state import Posting

NOW = datetime(2026, 10, 13, tzinfo=UTC)
COMPANIES = {
    "acme": Company(id="acme", name="Acme", adapter="ultipro", url="u", added=date(2026, 1, 1)),
    "mus": Company(
        id="mus", name="Board", adapter="moveupstatesc", url="u", added=date(2026, 1, 1)
    ),
}


def posting(key: str, employer: str = "", check: str = "ok") -> Posting:
    cid, sid = key.split(":")
    return Posting(
        key=key,
        company_id=cid,
        source_id=sid,
        title="Dev",
        location="Greenville, SC",
        url="u",
        posted_date=None,
        first_seen=NOW,
        last_seen=NOW,
        employer=employer,
        classification={"employer_check": check},
    )


@pytest.mark.parametrize(
    ("a", "b"),
    [("AFL", "afl"), ("Fluor Corp.", "FLUOR"), ("SWJ TECHNOLOGY, LLC", "SWJ Technology")],
)
def test_employer_key_normalizes_names(a, b):
    assert employers.employer_key(posting("mus:1", a)) == employers.employer_key(
        posting("mus:2", b)
    )


def test_posting_without_employer_is_its_own_key():
    assert employers.employer_key(posting("mus:1")) == "posting:mus:1"


def test_status():
    decisions = {}
    assert employers.status(posting("acme:1", check="verify"), COMPANIES, decisions) == "trusted"
    assert employers.status(posting("mus:1", "X", "ok"), COMPANIES, decisions) == "ok"
    flagged = posting("mus:2", "Shady LLC", "verify")
    assert employers.status(flagged, COMPANIES, decisions) == "unverified"

    key = employers.employer_key(flagged)
    employers.decide(decisions, key, "Shady LLC", "approved")
    assert employers.status(flagged, COMPANIES, decisions) == "approved"
    employers.decide(decisions, key, "Shady LLC", "blocked")
    assert employers.status(flagged, COMPANIES, decisions) == "blocked"
    employers.decide(decisions, key, "Shady LLC", None)
    assert employers.status(flagged, COMPANIES, decisions) == "unverified"


def test_round_trip_and_bad_decision(tmp_path):
    path = tmp_path / "employers.json"
    d = jsonfile.read(path)
    employers.decide(d, "employer:afl", "AFL", "approved")
    jsonfile.write(path, d)
    assert jsonfile.read(path)["employer:afl"]["status"] == "approved"
    with pytest.raises(ValueError):
        employers.decide(d, "employer:afl", "AFL", "maybe")
