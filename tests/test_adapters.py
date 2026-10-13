import json
from datetime import date

import httpx
import pytest
from conftest import fixture_text

from jobscraper.adapters import michelin, successfactors, ultipro

MICHELIN_URL = "https://jobs.michelinman.com/job-offer-result-list?criteria=%7B%7D&page=1"
ULTIPRO_URL = "https://recruiting.ultipro.com/ABC/JobBoard/123/"


def mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- SuccessFactors (Arthrex) ---

ARTHREX_URL = "https://careers.arthrex.com/go/Jobs-in-South-Carolina/7566200/"


def test_successfactors_parses_tiles():
    postings, total = successfactors.parse(fixture_text("arthrex.html"), ARTHREX_URL)
    assert total == 18
    assert len(postings) == 18
    assert len({p.source_id for p in postings}) == 18
    first = postings[0]
    assert first.source_id == "1358704157"
    assert first.title == "Supervisor - Calibration & Metrology"
    assert first.location == "Pendleton, SC, US, 29670"
    assert first.category == "Manufacturing"
    assert first.url == (
        "https://careers.arthrex.com/NorthAmerica/job/"
        "Pendleton-Supervisor-Calibration-&-Metrology-SC-29670/1358704157/"
    )


def test_successfactors_fails_when_tiles_missing(make_company):
    html = fixture_text("arthrex.html").replace("of 18 Jobs", "of 40 Jobs")
    client = mock_client(lambda request: httpx.Response(200, text=html))
    with pytest.raises(ValueError, match="40 jobs"):
        successfactors.fetch(make_company("successfactors", ARTHREX_URL), client)


# --- Michelin ---


def test_michelin_parses_cards_and_pager():
    postings, last_page = michelin.parse(fixture_text("michelin_p1.html"), MICHELIN_URL)
    assert last_page == 1
    assert len(postings) == 4
    first = postings[0]
    assert first.source_id == "it-site-support-analyst"
    assert first.title == "IT Site Support Analyst"
    assert first.location == "ANDERSON, SC, United States of America"
    assert first.url == "https://jobs.michelinman.com/job-offer-result-list/it-site-support-analyst"
    assert first.posted_date == date(2026, 10, 5)
    assert first.category == "IS&Digital"


def test_michelin_follows_pages_and_dedupes(make_company):
    pages = {
        "1": fixture_text("michelin_multi_p1.html"),
        "2": fixture_text("michelin_multi_p2.html"),
    }
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params["page"]
        requested.append(page)
        # Pages 3-4 reuse page 2's content; dedupe should absorb it.
        return httpx.Response(200, text=pages.get(page, pages["2"]))

    company = make_company("michelin", MICHELIN_URL)
    postings = michelin.fetch(company, mock_client(handler), delay=0)

    assert requested == ["1", "2", "3", "4"]
    ids = [p.source_id for p in postings]
    assert len(ids) == len(set(ids))
    assert "industry-4-0-manager" in ids  # on both pages, kept once
    assert len(ids) == 17


# --- UltiPro (ScanSource) ---


def test_ultipro_filters_by_state():
    data = json.loads(fixture_text("scansource.json"))
    all_postings = ultipro.parse(data, ULTIPRO_URL)
    sc_postings = ultipro.parse(data, ULTIPRO_URL, ("SC",))
    assert len(all_postings) == 50
    assert len(sc_postings) == 36
    assert all("SC" in p.location for p in sc_postings)
    p = sc_postings[0]
    assert "  " not in p.title
    assert p.category
    assert p.summary and "\n" not in p.summary
    assert p.url == f"{ULTIPRO_URL.rstrip('/')}/OpportunityDetail?opportunityId={p.source_id}"


def test_ultipro_keeps_remote_jobs_outside_states():
    data = json.loads(fixture_text("scansource.json"))
    out_of_state = next(
        o
        for o in data["opportunities"]
        if all(loc["Address"]["State"]["Code"] != "SC" for loc in o["Locations"])
    )
    no_locations = {**out_of_state, "Id": "no-loc", "Locations": []}
    remote_title = {**out_of_state, "Id": "remote-title", "Title": "Developer (Remote)"}
    data = {"opportunities": [out_of_state, no_locations, remote_title], "totalCount": 3}
    postings = ultipro.parse(data, ULTIPRO_URL, ("SC",))
    assert [p.source_id for p in postings] == ["no-loc", "remote-title"]
    assert postings[0].location == "Remote"


def test_ultipro_paginates_until_total(make_company):
    data = json.loads(fixture_text("scansource.json"))
    second_page = {"opportunities": data["opportunities"][:2], "totalCount": 52}
    second_page["opportunities"] = [
        {**o, "Id": f"extra-{i}"} for i, o in enumerate(second_page["opportunities"])
    ]
    skips = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/JobBoardView/LoadSearchResults")
        skip = json.loads(request.content)["opportunitySearch"]["Skip"]
        skips.append(skip)
        return httpx.Response(200, json=data if skip == 0 else second_page)

    company = make_company("ultipro", ULTIPRO_URL)
    postings = ultipro.fetch(company, mock_client(handler), delay=0)
    assert skips == [0, 50]
    assert len(postings) == 52
