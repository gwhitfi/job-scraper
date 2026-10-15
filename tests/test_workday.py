import json
from datetime import date

import httpx
import pytest
from conftest import fixture_text, mock_client

from jobscraper import crawl, discovery
from jobscraper.adapters import workday
from jobscraper.models import Company


def test_workday_discovery_paginates_and_preserves_search():
    seen = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        assert request.url.path == "/wday/cxs/acme/External/jobs"
        body = json.loads(request.content)
        seen.append(body)
        offset = body["offset"]
        return httpx.Response(
            200,
            json={
                "total": 3 if offset == 0 else 0,
                "jobPostings": [
                    {
                        "title": "Developer",
                        "externalPath": f"/job/Greenville/Developer_R{offset}",
                        "locationsText": "Greenville, SC",
                    }
                ],
            },
        )

    client = crawl.Client(mock_client(handler), validate=lambda url: None, sleep=lambda d: None)
    result = discovery.inspect(
        "https://acme.wd1.myworkdayjobs.com/en-US/External?q=dev&locations=SC", client
    )
    assert result.company.options["strategy"] == "workday"
    assert [p.source_id for p in result.postings] == ["R0", "R1", "R2"]
    assert (
        result.postings[0].url
        == "https://acme.wd1.myworkdayjobs.com/External/job/Greenville/Developer_R0"
    )
    assert [b["offset"] for b in seen] == [0, 1, 2]
    assert all(
        b["searchText"] == "dev" and b["appliedFacets"] == {"locations": ["SC"]} for b in seen
    )


def test_workday_refuses_incomplete_results():
    company = Company(
        "acme", "Acme", "workday", "https://acme.wd1.myworkdayjobs.com/External", date(2026, 10, 14)
    )
    client = mock_client(lambda r: httpx.Response(200, json={"total": 20, "jobPostings": []}))
    with pytest.raises(ValueError, match="pagination stopped"):
        workday.fetch(company, client)


def test_workday_unsupported_filter_is_not_silently_dropped():
    company = Company(
        "acme",
        "Acme",
        "workday",
        "https://acme.wd1.myworkdayjobs.com/External?bad=x",
        date(2026, 10, 14),
    )
    with pytest.raises(ValueError, match="Unsupported Workday"):
        workday.fetch(company, mock_client(lambda r: pytest.fail("Unexpected request")))


def test_workday_refuses_capped_total():
    company = Company(
        "acme", "Acme", "workday", "https://acme.wd1.myworkdayjobs.com/External", date(2026, 10, 14)
    )
    client = mock_client(lambda r: httpx.Response(200, json={"total": 2000, "jobPostings": []}))
    with pytest.raises(ValueError, match="caps large searches"):
        workday.fetch(company, client)


def test_workday_state_scope_includes_remote_and_refreshes_facet_ids():
    company = Company(
        "acme",
        "Acme",
        "workday",
        "https://acme.wd1.myworkdayjobs.com/External",
        date(2026, 10, 14),
        options={"states": ["SC"]},
    )
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        filters = body["appliedFacets"]
        if not filters:
            return httpx.Response(
                200,
                json={
                    "total": 2000,
                    "jobPostings": [],
                    "facets": [
                        {
                            "facetParameter": "State",
                            "values": [{"descriptor": "South Carolina", "id": "sc-new"}],
                        },
                        {
                            "facetParameter": "remoteType",
                            "values": [{"descriptor": "Virtual", "id": "remote-new"}],
                        },
                    ],
                },
            )
        sid = "1" if "State" in filters else "2"
        return httpx.Response(
            200,
            json={
                "total": 1,
                "jobPostings": [
                    {
                        "title": "Developer",
                        "externalPath": f"/job/City/Developer_R{sid}",
                        "locationsText": "SC" if sid == "1" else "CA",
                    }
                ],
            },
        )

    result = workday.fetch(company, mock_client(handler))
    assert len(result) == 2
    assert calls[1]["appliedFacets"] == {"State": ["sc-new"]}
    assert calls[2]["appliedFacets"] == {"remoteType": ["remote-new"]}
    assert result[1].location == "CA; Remote"


@pytest.mark.parametrize("fixture", ["workday_duke.json", "workday_target_capped.json"])
def test_live_workday_facets_select_states_and_remote(fixture):
    data = json.loads(fixture_text(fixture))
    company = Company(
        "acme",
        "Acme",
        "workday",
        "https://acme.wd1.myworkdayjobs.com/External",
        date(2026, 10, 14),
        options={"states": ["SC", "NC", "GA"]},
    )
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if not body["appliedFacets"]:
            return httpx.Response(200, json=data)
        return httpx.Response(200, json={"total": 1, "jobPostings": data["jobPostings"][:1]})

    postings = workday.fetch(company, mock_client(handler))
    assert len(postings) == 1  # State and remote searches can overlap.
    assert len(calls) == 3
    state_key = next(iter(calls[1]["appliedFacets"]))
    assert state_key in ("State", "Location_Region_State_Province")
    assert "remoteType" in calls[2]["appliedFacets"]
    assert "Remote" in postings[0].location
