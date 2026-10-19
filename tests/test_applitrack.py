from datetime import date

import httpx
import pytest
from conftest import fixture_text, mock_client

from jobscraper.adapters import applitrack
from jobscraper.scraping import crawl, discovery

URL = "https://www.applitrack.com/acsd3/onlineapp/default.aspx?all=1"


def test_saved_listings_have_stable_ids_dates_and_descriptions():
    jobs = applitrack.parse(fixture_text("applitrack_acsd3_output.js"), URL)
    assert [p.source_id for p in jobs] == ["293", "294", "262"]
    assert jobs[0].title == "Custodian - Immediate Opening"
    assert jobs[0].location == "Starr-Iva Middle School"
    assert jobs[0].posted_date == date(2026, 10, 2)
    assert jobs[0].category == "Custodial Services/ Custodian"
    assert "dedicated and dependable" in jobs[0].summary
    assert jobs[0].url == URL.split("?")[0] + "?AppliTrackJobID=293"


@pytest.mark.parametrize(
    "original,replacement",
    [("<b>3</b> openings", "<b>4</b> openings"), ("p294_", "p293_")],
)
def test_incomplete_or_duplicate_listings_fail(original, replacement):
    script = fixture_text("applitrack_acsd3_output.js").replace(original, replacement)
    with pytest.raises(ValueError, match="incomplete|duplicate"):
        applitrack.parse(script, URL)


def test_unrecognized_script_cannot_be_accepted_as_empty():
    with pytest.raises(ValueError, match="count"):
        applitrack.parse("document.write('Service unavailable');", URL)


def test_zero_openings_requires_explicit_total():
    script = "document.write('<div id=\"AppliTrackSearchContainer\">(0 openings)</div>');"
    assert applitrack.parse(script, URL) == []


def test_original_url_discovers_script_without_broken_fallback_or_other_boards():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        assert request.url.params["all"] == "1"
        if request.url.path == "/acsd3/onlineapp/default.aspx":
            return httpx.Response(200, text=fixture_text("applitrack_acsd3_shell.html"))
        assert request.url.path == "/acsd3/onlineapp/jobpostings/Output.asp"
        return httpx.Response(200, text=fixture_text("applitrack_acsd3_output.js"))

    client = crawl.Client(mock_client(handler), validate=lambda u: None, sleep=lambda d: None)
    result = discovery.inspect(URL, client)
    assert result.status == "added"
    assert result.company.options["strategy"] == "applitrack"
    assert result.company.name == "Anderson School District 3 - Frontline Recruitment"
    assert len(result.postings) == 3
    assert len(requests) == 3


def test_script_decodes_escaped_apostrophes_and_unicode():
    script = fixture_text("applitrack_acsd3_output.js").replace(
        "Custodian - Immediate Opening", r"Custodian\'s \u2013 Immediate Opening"
    )
    assert applitrack.parse(script, URL)[0].title == "Custodian's – Immediate Opening"
