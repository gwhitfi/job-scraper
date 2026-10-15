import json
from dataclasses import replace

import httpx
from conftest import fixture_text, mock_client

from jobscraper.adapters import phenom, workday
from jobscraper.geography import locations


def test_synnex_countries_are_not_lost():
    jobs, _ = phenom.parse(json.loads(fixture_text("synnex_locations.json")), "https://x")
    scoped = locations.build_filter(("SC", "NC", "GA"), {})
    kept, dropped = locations.apply(jobs, scoped)
    assert len(dropped) == len(jobs)
    assert kept == []
    assert jobs[0].countries == ("Portugal",)
    assert "Portugal" in jobs[0].location


def test_spartanburg_local_jobs_really_pass_geography():
    jobs, _ = phenom.parse(json.loads(fixture_text("spartanburg_locations.json")), "https://x")
    kept, _ = locations.apply(jobs, locations.build_filter(("SC",), {}))
    assert len(kept) == 50
    assert all(locations.places(p.location) for p in kept)


def test_multi_location_job_keeps_local_alternative():
    data = {
        "refineSearch": {
            "data": {
                "jobs": [
                    {
                        "jobSeqNo": "1",
                        "title": "Dev",
                        "city": "Lisboa",
                        "state": "Lisbon",
                        "country": "Portugal",
                        "multi_location_array": [
                            {"city": "Lisboa", "state": "Lisbon", "country": "Portugal"},
                            {
                                "city": "Greenville",
                                "state": "South Carolina",
                                "country": "United States",
                            },
                        ],
                    }
                ]
            },
            "totalHits": 1,
        }
    }
    jobs, _ = phenom.parse(data, "https://x")
    assert locations.matches(jobs[0], locations.build_filter(("Greenville, SC",), {}))


def test_td_nested_facets_scope_country_locations_and_remote(make_company):
    data = json.loads(fixture_text("workday_td_locations.json"))
    company = replace(
        make_company("workday", "https://td.wd3.myworkdayjobs.com/TD_Bank_Careers"),
        options={"states": ["SC", "NC", "GA"]},
    )
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if not body["appliedFacets"]:
            return httpx.Response(200, json=data)
        job = data["jobPostings"][0] | {"locationsText": "Greenville, South Carolina"}
        return httpx.Response(200, json={"total": 1, "jobPostings": [job]})

    jobs = workday.fetch(company, mock_client(handler))
    assert len(calls) == 3
    assert all("locationCountry" in body["appliedFacets"] for body in calls[1:])
    assert "locations" in calls[1]["appliedFacets"]
    assert "remoteType" in calls[2]["appliedFacets"]
    facets = workday.facets_in(data)
    options = next(f["values"] for f in facets if f["facetParameter"] == "locations")
    selected = calls[1]["appliedFacets"]["locations"]
    assert selected
    assert all(
        any(st in ("SC", "NC", "GA") for _, st in locations.places(v["descriptor"]))
        for v in options
        if v["id"] in selected
    )
    assert jobs[0].countries == ("US",)


def test_workday_resolves_hidden_locations_and_does_not_call_office_remote(make_company):
    company = make_company("workday", "https://td.wd3.myworkdayjobs.com/TD_Bank_Careers")
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "total": 1,
                    "jobPostings": [
                        {
                            "title": "Developer",
                            "externalPath": "/job/Charlotte/Dev_R123",
                            "locationsText": "2 Locations",
                            "remoteType": "Office Focused",
                        }
                    ],
                },
            )
        assert request.url.path == "/wday/cxs/td/TD_Bank_Careers/job/Charlotte/Dev_R123"
        return httpx.Response(200, json=json.loads(fixture_text("workday_td_detail.json")))

    job = workday.fetch(company, mock_client(handler))[0]
    assert len(requests) == 2
    assert locations.places(job.location) == [("charlotte", "NC"), ("mount laurel", "NJ")]
    assert not locations.matches(job, locations.build_filter(("Greenville, SC",), {}))
    assert locations.matches(job, locations.build_filter(("Charlotte, NC",), {}))
    assert not locations.is_remote(job)
