"""Reusable extraction recipes and completeness guarantees, using offline responses."""

import json
from datetime import date

import httpx
import pytest
from conftest import mock_client

from jobscraper.adapters import generic
from jobscraper.models import Company

URL = "https://example.com/feed"
FIELDS = {"source_id": "id", "title": "title", "url": "url"}


def source(**options):
    return Company("board", "Board", "generic", URL, date(2026, 1, 1), options=options)


def job(id="1", **values):
    return {"id": id, "title": "Developer", "url": f"/jobs/{id}", **values}


def test_json_preserves_employer_and_countries():
    company = source(
        format="json",
        items="jobs",
        fields=FIELDS
        | {
            "employer": "company",
            "countries": "countries",
        },
    )
    client = mock_client(
        lambda r: httpx.Response(
            200,
            json={
                "jobs": [
                    job(
                        company={"name": "Acme"},
                        countries=[{"name": "US"}, "Canada", "US"],
                    )
                ]
            },
        )
    )
    (posting,) = generic.fetch(company, client)
    assert posting.employer == "Acme"
    assert posting.countries == ("US", "Canada")


def test_html_preserves_board_metadata():
    postings, _ = generic.parse_html(
        '<article><a href="/jobs/1">Developer</a><b>Acme</b><i>Canada</i></article>',
        URL,
        {
            "format": "html",
            "item": "article",
            "fields": {
                "title": "a",
                "url": "a@href",
                "employer": "b",
                "countries": "i",
            },
        },
    )
    assert postings[0].employer == "Acme"
    assert postings[0].countries == ("Canada",)


def test_jsonld_preserves_board_metadata():
    html = (
        '<script type="application/ld+json">'
        + json.dumps(
            {
                "@type": "JobPosting",
                "title": "Developer",
                "url": "/jobs/1",
                "hiringOrganization": {"@type": "Organization", "name": "Acme"},
                "jobLocation": {"address": {"addressLocality": "London", "addressCountry": "GB"}},
            }
        )
        + "</script>"
    )
    (posting,) = generic.jsonld(html, URL)
    assert posting.employer == "Acme"
    assert posting.countries == ("GB",)


def test_jsonld_ambiguous_listing_ids_fail_instead_of_dropping_jobs():
    html = (
        '<script type="application/ld+json">'
        + json.dumps(
            [{"@type": "JobPosting", "title": title} for title in ("Developer", "Analyst")]
        )
        + "</script>"
    )
    client = mock_client(lambda r: httpx.Response(200, text=html))
    with pytest.raises(ValueError, match="share an identifier"):
        generic.fetch(source(format="jsonld"), client)


@pytest.mark.parametrize("total", [-1, True, "1", None])
def test_invalid_totals_are_not_successful_fetches(total):
    client = mock_client(lambda r: httpx.Response(200, json={"jobs": [job()], "total": total}))
    with pytest.raises(ValueError, match="expected total"):
        generic.fetch(source(format="json", items="jobs", fields=FIELDS, total="total"), client)


def test_shrinking_total_does_not_accept_partial_results():
    def respond(request):
        second = request.url.params.get("offset") == "1"
        return httpx.Response(
            200, json={"jobs": [job("2" if second else "1")], "total": 2 if second else 3}
        )

    with pytest.raises(ValueError, match="total changed"):
        generic.fetch(
            source(format="json", items="jobs", fields=FIELDS, total="total", page_field="offset"),
            mock_client(respond),
        )


def test_more_jobs_than_reported_total_fails():
    client = mock_client(lambda r: httpx.Response(200, json={"jobs": [job()], "total": 0}))
    with pytest.raises(ValueError, match="more jobs than"):
        generic.fetch(source(format="json", items="jobs", fields=FIELDS, total="total"), client)


def test_json_links_resolve_against_redirect_destination():
    seen = []

    def respond(request):
        seen.append(str(request.url))
        if request.url.path == "/feed":
            return httpx.Response(302, headers={"location": "/api/jobs/"})
        return httpx.Response(200, json={"jobs": [job(url="1")]})

    with httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
        (posting,) = generic.fetch(source(format="json", items="jobs", fields=FIELDS), client)
    assert posting.url == "https://example.com/api/jobs/1"


def test_offset_pagination_preserves_empty_query_filters():
    def respond(request):
        assert "department" in request.url.params
        assert request.url.params["department"] == ""
        return httpx.Response(200, json={"jobs": [job()], "total": 1})

    company = Company(
        "x",
        "X",
        "generic",
        URL + "?department=",
        date(2026, 1, 1),
        options={
            "format": "json",
            "items": "jobs",
            "fields": FIELDS,
            "total": "total",
            "page_field": "offset",
        },
    )
    assert len(generic.fetch(company, mock_client(respond))) == 1


def test_explicit_foreign_country_does_not_match_us_remote_filter():
    from jobscraper.geography import locations

    posting = generic.posting(
        {"title": "Dev", "url": "/jobs/1", "location": "Remote", "countries": "Canada"}, URL
    )
    assert not locations.matches(posting, locations.build_filter(("SC",), {}))


@pytest.mark.parametrize(
    "field,value", [("employer", "Other Co"), ("location", "Other City"), ("countries", "CA")]
)
def test_conflicting_metadata_cannot_silently_collapse_jobs(field, value):
    fields = FIELDS | {field: field}
    client = mock_client(
        lambda r: httpx.Response(200, json={"jobs": [job(), job(**{field: value})]})
    )
    with pytest.raises(ValueError, match="share an identifier"):
        generic.fetch(source(format="json", items="jobs", fields=fields), client)


def test_relative_next_link_resolves_against_redirect_destination():
    seen = []

    def respond(request):
        seen.append(str(request.url))
        if request.url.path == "/feed":
            return httpx.Response(302, headers={"location": "/api/jobs/"})
        if request.url.path == "/api/jobs/":
            return httpx.Response(200, json={"jobs": [job()], "next": "page2", "total": 2})
        assert request.url.path == "/api/jobs/page2"
        return httpx.Response(200, json={"jobs": [job("2")], "next": None, "total": 2})

    with httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
        postings = generic.fetch(
            source(format="json", items="jobs", fields=FIELDS, next="next", total="total"), client
        )
    assert [p.source_id for p in postings] == ["1", "2"]
    assert seen[-1] == "https://example.com/api/jobs/page2"


def test_javascript_next_button_is_not_accepted_as_complete_list():
    from conftest import fixture_text

    from jobscraper.scraping import discovery

    html = fixture_text("kaseya_jobs.html")
    with pytest.raises(ValueError, match="Jobs load through a button"):
        generic.parse_html(html, URL, discovery.infer_html(html, URL))


def test_disabled_next_button_allows_final_page():
    html = '<a class="job" href="/jobs/1">Developer</a><button disabled>Next</button>'
    postings, next_url = generic.parse_html(
        html,
        URL,
        {
            "format": "html",
            "item": "a.job",
            "fields": {"title": "", "url": "@href"},
        },
    )
    assert len(postings) == 1 and next_url is None


def test_greenhouse_feed_can_preserve_existing_html_url_ids():
    from conftest import fixture_text

    data = json.loads(fixture_text("kaseya_greenhouse.json"))
    fields = {"title": "title", "url": "absolute_url", "location": "location.name"}
    client = mock_client(lambda r: httpx.Response(200, json=data))
    postings = generic.fetch(
        source(format="json", items="jobs", total="meta.total", fields=fields), client
    )
    for item, posting in zip(data["jobs"], postings, strict=True):
        old = generic.posting({"title": item["title"], "url": item["absolute_url"]}, URL)
        assert posting.source_id == old.source_id
