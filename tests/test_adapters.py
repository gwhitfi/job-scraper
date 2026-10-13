import json
from datetime import date, timedelta

import httpx
import pytest
from conftest import fixture_text

from jobscraper.adapters import michelin, moveupstatesc, successfactors, ultipro

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


def test_successfactors_pages_through_more_results(make_company):
    page = fixture_text("arthrex.html").replace("of 18 Jobs", "of 20 Jobs")
    extra = fixture_text("arthrex_tiles.html")
    # The tiles endpoint returns two jobs the first page didn't have.
    extra = extra.replace("1358704157", "9000000001").replace("1372720257", "9000000002")
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            200, text=extra if "tile-search-results" in request.url.path else page
        )

    postings = successfactors.fetch(
        make_company("successfactors", ARTHREX_URL), mock_client(handler), delay=0
    )
    assert len(postings) == 20
    assert requested[1] == (
        "https://careers.arthrex.com/tile-search-results/category/7566200/?startrow=18"
    )


def test_successfactors_fails_when_paging_stalls(make_company):
    page = fixture_text("arthrex.html").replace("of 18 Jobs", "of 40 Jobs")
    client = mock_client(lambda request: httpx.Response(200, text=page))
    with pytest.raises(ValueError, match="paging stopped at 18"):
        successfactors.fetch(make_company("successfactors", ARTHREX_URL), client, delay=0)


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


def test_michelin_follows_pages_and_dedupes(make_company, caplog):
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
    assert "repeated across pages" in caplog.text


# --- UltiPro (ScanSource) ---


def test_ultipro_parses_all_locations():
    data = json.loads(fixture_text("scansource.json"))
    postings = ultipro.parse(data, ULTIPRO_URL)
    assert len(postings) == 50
    assert sum(", SC" in p.location for p in postings) == 36
    p = next(p for p in postings if ", SC" in p.location)
    assert "  " not in p.title
    assert p.category
    assert p.summary and "\n" not in p.summary
    assert p.url == f"{ULTIPRO_URL.rstrip('/')}/OpportunityDetail?opportunityId={p.source_id}"


def test_ultipro_marks_remote_jobs():
    data = json.loads(fixture_text("scansource.json"))
    onsite = data["opportunities"][0]
    no_locations = {**onsite, "Id": "no-loc", "Locations": []}
    remote_title = {**onsite, "Id": "remote-title", "Title": "Developer (Remote)"}
    data = {"opportunities": [onsite, no_locations, remote_title], "totalCount": 3}
    by_id = {p.source_id: p for p in ultipro.parse(data, ULTIPRO_URL)}
    assert "remote" not in by_id[onsite["Id"]].location.lower()
    assert by_id["no-loc"].location == "Remote"
    assert by_id["remote-title"].location.endswith("Remote")


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


# --- Move Upstate SC (job board) ---

MUS_URL = "https://moveupstatesc.com/find-a-job/it-tech-development/"
TODAY = date(2026, 10, 13)


def test_moveupstatesc_parses_cards_and_pager():
    postings, last_page = moveupstatesc.parse(fixture_text("moveupstatesc_p1.html"), TODAY)
    assert last_page == 5
    assert len(postings) == 60
    assert len({p.source_id for p in postings}) == 60
    first = postings[0]
    assert first.title == "Business Intelligence Developer"
    assert first.employer == "AFL"
    assert first.location == "Duncan, SC 29334"
    assert first.posted_date == date(2026, 10, 10)
    assert first.url.startswith("https://www.indeed.com/viewjob?jk=")
    assert first.source_id == first.url.split("jk=")[1]
    assert first.summary.startswith("AFL manufactures")
    assert all("Posted" not in p.title for p in postings)
    assert any(p.employer == "" and p.location == "Greenville, SC 29601" for p in postings)


@pytest.mark.parametrize(
    ("text", "days"),
    [
        ("Posted today", 0),
        ("Just posted", 0),
        ("Posted 1 day ago", 1),
        ("Posted 6 days ago", 6),
        ("Posted 2 weeks ago", 14),
        ("Posted 1 month ago", 30),
        ("Posted 30+ days ago", 30),
    ],
)
def test_moveupstatesc_relative_dates(text, days):
    assert moveupstatesc.posted_date(text, TODAY) == TODAY - timedelta(days=days)


def test_moveupstatesc_unknown_date():
    assert moveupstatesc.posted_date("Posted recently", TODAY) is None


def test_moveupstatesc_follows_pages(make_company):
    html = fixture_text("moveupstatesc_p1.html")
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        # Every page returns the same cards; dedupe keeps one copy.
        return httpx.Response(200, text=html)

    company = make_company("moveupstatesc", MUS_URL)
    postings = moveupstatesc.fetch(company, mock_client(handler), delay=0)
    assert requested == [
        "/find-a-job/it-tech-development/",
        "/find-a-job/it-tech-development/page/2/",
        "/find-a-job/it-tech-development/page/3/",
        "/find-a-job/it-tech-development/page/4/",
        "/find-a-job/it-tech-development/page/5/",
    ]
    assert len(postings) == 60


def test_moveupstatesc_retries_just_the_failed_page(make_company):
    html = fixture_text("moveupstatesc_p1.html")
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path.endswith("/page/3/") and requested.count(request.url.path) == 1:
            return httpx.Response(503)
        return httpx.Response(200, text=html)

    company = make_company("moveupstatesc", MUS_URL)
    moveupstatesc.fetch(company, mock_client(handler), delay=0)
    assert requested.count("/find-a-job/it-tech-development/") == 1  # page 1 not refetched
    assert requested.count("/find-a-job/it-tech-development/page/3/") == 2


def test_moveupstatesc_gives_up_after_repeated_failures(make_company):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    company = make_company("moveupstatesc", MUS_URL)
    with pytest.raises(httpx.HTTPStatusError):
        moveupstatesc.fetch(company, mock_client(handler), delay=0)
