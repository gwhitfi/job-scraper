import json
from datetime import date

import httpx
import pytest
from conftest import fixture_text, mock_client

from jobscraper.adapters import workday
from jobscraper.models import Company
from jobscraper.scraping import crawl, discovery


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


def test_workday_dsg_detail_url_preserves_technology_category():
    url = (
        "https://dickssportinggoods.wd1.myworkdayjobs.com/en-US/DSG/details/"
        "Domain-Architect--Technology-Integration-Management-Office-_202626126"
        "?CF_-_Job_Requisition_Job_Category__LRV__Extended=451b0d7174c41003d1e5ef1fc89d0000"
    )
    calls = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        assert request.method == "POST"
        assert request.url.path == "/wday/cxs/dickssportinggoods/DSG/jobs"
        body = json.loads(request.content)
        calls.append(body)
        assert body["appliedFacets"] == {
            "CF_-_Job_Requisition_Job_Category__LRV__Extended": ["451b0d7174c41003d1e5ef1fc89d0000"]
        }
        return httpx.Response(200, json=json.loads(fixture_text("workday_dsg_technology.json")))

    client = crawl.Client(mock_client(handler), validate=lambda u: None, sleep=lambda d: None)
    result = discovery.inspect(url, client, name="Dick's Sporting Goods", states=("SC", "NC", "GA"))
    assert result.status == "added"
    assert result.company.url == (
        "https://dickssportinggoods.wd1.myworkdayjobs.com/DSG"
        "?CF_-_Job_Requisition_Job_Category__LRV__Extended=451b0d7174c41003d1e5ef1fc89d0000"
    )
    assert len(result.postings) == len({p.source_id for p in result.postings}) == 9
    assert result.postings[0].source_id == "202626126"
    assert result.postings[0].location == "Remote - US"
    assert any(p.source_id == "202622444" and not p.location for p in result.postings)
    assert len(calls) == 1


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


@pytest.mark.parametrize(
    "state_key", ["State", "Location_Region_State_Province", "locationRegionStateProvince"]
)
def test_workday_state_scope_includes_remote_and_refreshes_facet_ids(state_key):
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
                            "facetParameter": state_key,
                            "values": [{"descriptor": "South Carolina", "id": "sc-new"}],
                        },
                        {
                            "facetParameter": "remoteType",
                            "values": [{"descriptor": "Virtual", "id": "remote-new"}],
                        },
                    ],
                },
            )
        sid = "1" if state_key in filters else "2"
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
    assert calls[1]["appliedFacets"] == {state_key: ["sc-new"]}
    assert calls[2]["appliedFacets"] == {"remoteType": ["remote-new"]}
    assert result[1].location == "CA; Remote"


def test_workday_hcmportal_preserves_explicit_state_with_form_regions():
    url = (
        "https://hcmportal.wd5.myworkdayjobs.com/search/"
        "?locationRegionStateProvince=1d2632a43d204a2799bcd38b57e8a26e"
    )
    calls = []
    data = json.loads(fixture_text("workday_ups_sc.json"))
    data["facets"].append(
        {"facetParameter": "remoteType", "values": [{"id": "remote", "descriptor": "Remote"}]}
    )

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        assert request.url.path == "/wday/cxs/hcmportal/search/jobs"
        body = json.loads(request.content)
        calls.append(body)
        assert body["appliedFacets"]["locationRegionStateProvince"] == [
            "1d2632a43d204a2799bcd38b57e8a26e"
        ]
        return httpx.Response(200, json=data)

    client = crawl.Client(mock_client(handler), validate=lambda u: None, sleep=lambda d: None)
    result = discovery.inspect(url, client, name="UPS", states=("SC", "NC", "GA"))
    assert result.status == "added"
    assert len(result.postings) == len({p.source_id for p in result.postings}) == 12
    assert len(calls) == 2
    assert calls[-1]["appliedFacets"] == {
        "locationRegionStateProvince": ["1d2632a43d204a2799bcd38b57e8a26e"],
        "locationCountry": ["bc33aa3152ec42d4995f4791a106ed09"],
    }
    assert result.postings[0].source_id == "R26036919"


@pytest.mark.parametrize("query", ["geotagText=US%2F29621%2FAnderson", "distance=radius"])
def test_workday_incomplete_distance_filter_is_rejected(make_company, query):
    company = make_company(
        "workday",
        "https://hcmportal.wd5.myworkdayjobs.com/search?" + query,
    )
    with pytest.raises(ValueError, match="require both distance and geotagText"):
        workday.fetch(company, mock_client(lambda r: pytest.fail("Unexpected request")))


def test_workday_ups_radius_search_preserves_filters_and_paginates(make_company):
    url = (
        "https://hcmportal.wd5.myworkdayjobs.com/search/"
        "?geotagText=US%2F29621%2FAnderson&distance=a538cdb60a631000078a2c71efcc00f6"
    )
    company = make_company("workday", url, options={"states": ["SC", "NC", "GA"]})
    calls = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"jobPostingInfo": {"location": "Atlanta, GA"}})
        body = json.loads(request.content)
        calls.append(body)
        assert body["appliedFacets"]["distance"] == ["a538cdb60a631000078a2c71efcc00f6"]
        assert body["appliedFacets"]["geotagText"] == ["US/29621/Anderson"]
        assert "locationRegionStateProvince" not in body["appliedFacets"]
        data = json.loads(fixture_text(f"workday_ups_radius_{body['offset']}.json"))
        # Even if the platform exposes remote scopes, retain the explicit radius search.
        data["facets"] = [
            {
                "facetParameter": "locationRegionStateProvince",
                "values": [{"id": "sc", "descriptor": "South Carolina"}],
            },
            {"facetParameter": "remoteType", "values": [{"id": "remote", "descriptor": "Remote"}]},
        ]
        return httpx.Response(200, json=data)

    postings = workday.fetch(company, mock_client(handler))
    assert len(postings) == len({p.source_id for p in postings}) == 26
    assert [body["offset"] for body in calls] == [0, 20]


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
        job = data["jobPostings"][0] | {
            "locationsText": "Greenville, SC",
            "remoteType": "Remote" if "remoteType" in body["appliedFacets"] else "Onsite",
        }
        return httpx.Response(200, json={"total": 1, "jobPostings": [job]})

    postings = workday.fetch(company, mock_client(handler))
    assert len(postings) == 1  # State and remote searches can overlap.
    assert len(calls) == 3
    state_key = next(iter(calls[1]["appliedFacets"]))
    assert state_key in ("State", "Location_Region_State_Province")
    assert "remoteType" in calls[2]["appliedFacets"]
    assert "Remote" in postings[0].location
