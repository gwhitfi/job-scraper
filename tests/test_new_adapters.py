"""Adapters added for the second batch of companies, tested against saved real responses."""

import json
from datetime import date

import httpx
import pytest
from conftest import fixture_text, mock_client

from jobscraper.adapters import (
    bamboohr,
    bmw,
    healthcaresource,
    html_list,
    oracle_hcm,
    paylocity,
    phenom,
    prisma,
)
from jobscraper.models import Company


def company(adapter: str, url: str, **options) -> Company:
    return Company(
        id="x", name="X Co", adapter=adapter, url=url, added=date(2026, 1, 1), options=options
    )


# --- html_list ---


def test_html_list_reads_cards():
    c = company(
        "html_list",
        "https://www.robojuice.com/careers",
        item=".w-dyn-item:has(a[href*='/job-postings/'])",
        title="h4",
        link="a[href*='/job-postings/']",
        summary=".plain-text-component",
    )
    (p,) = html_list.parse(fixture_text("robojuice.html"), c)
    assert p.title == "Senior Full-stack PHP Developer"
    assert p.url == "https://www.robojuice.com/job-postings/senior-full-stack-php-developer"
    assert p.summary.startswith("Build out")


def test_html_list_without_links_keys_on_title():
    c = company("html_list", "https://www.externalsoftware.com/contract-opportunities/", item="h3")
    (p,) = html_list.parse(fixture_text("externalsoftware.html"), c)
    assert (p.source_id, p.title, p.url) == ("front-end-developer", "Front-end Developer", c.url)


def test_html_list_none_text_means_no_jobs():
    c = company(
        "html_list",
        "https://www.simpledu.com/careers/",
        item="a.job",
        none_text="no open opportunities",
    )
    assert html_list.parse(fixture_text("simpledu.html"), c) == []


def test_html_list_flags_a_changed_page():
    c = company(
        "html_list",
        "https://www.simpledu.com/careers/",
        item="a.job",
        none_text="no open opportunities",
    )
    html = fixture_text("simpledu.html").replace("no open opportunities", "a new role")
    (p,) = html_list.parse(html, c)
    assert p.source_id == "careers-page-changed" and "check for openings" in p.title


# --- Paylocity / BambooHR ---


def test_paylocity():
    jobs = {p.title: p for p in paylocity.parse(fixture_text("paylocity_keymark.html"))}
    assert len(jobs) == 4
    p = jobs["Systems Engineer"]
    assert p.location == "Liberty, SC; Remote"
    assert p.posted_date == date(2026, 10, 1)
    assert p.url == "https://recruiting.paylocity.com/recruiting/jobs/Details/2277464"


def test_bamboohr_converts_state_names():
    (p,) = bamboohr.parse(
        json.loads(fixture_text("bamboohr_kopis.json")), "https://kp.bamboohr.com"
    )
    assert p.location == "Greenville, SC"
    assert p.url == "https://kp.bamboohr.com/careers/9"


# --- Prisma ---


def test_prisma_parses_cards():
    postings, total = prisma.parse(
        fixture_text("prisma_p1.html"), "https://careers.prismahealth.org/jobs/"
    )
    assert (total, len(postings)) == (42, 12)
    p = next(p for p in postings if p.source_id == "r1146666")
    assert p.title == "Technical Support Analyst, FT, Variable"
    assert p.location == "Greenville, SC"
    assert p.category == "Technical Group, Tech-IT/Technology"


def test_prisma_pages_until_total():
    pages = {"1": fixture_text("prisma_p1.html"), "2": fixture_text("prisma_p2.html")}
    requested = []

    def handler(request):
        page = request.url.params["page"]
        requested.append(page)
        return httpx.Response(200, text=pages.get(page, pages["2"]))

    url = "https://careers.prismahealth.org/jobs/?subteam=Management"
    postings = prisma.fetch(company("prisma", url), mock_client(handler), delay=0)
    assert requested == ["1", "2", "3", "4"]  # 42 jobs at 12 per page
    assert len(postings) == 24


# --- Phenom / Oracle / HealthcareSource / BMW ---


def test_phenom():
    postings, total = phenom.parse(
        json.loads(fixture_text("phenom_bonsecours.json")), "https://careers.bonsecours.com"
    )
    assert (total, len(postings)) == (171, 10)
    assert postings[0].location == "Greenville, SC"
    assert postings[0].url.startswith("https://careers.bonsecours.com/us/en/job/R280943/")


def test_phenom_runs_one_search_per_keyword():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"refineSearch": {"totalHits": 0, "data": {"jobs": []}}})

    c = company(
        "phenom",
        "https://careers.bonsecours.com/x",
        keywords=["analyst", "data"],
        states=["South Carolina"],
    )
    assert phenom.fetch(c, mock_client(handler), delay=0) == []
    assert [b["keywords"] for b in bodies] == ["analyst", "data"]
    assert bodies[0]["selected_fields"] == {"state": ["South Carolina"]}


def test_oracle_hcm():
    postings, total = oracle_hcm.parse(
        json.loads(fixture_text("oracle_vertiv.json")), "https://egup.fa.us2.oraclecloud.com", "CX"
    )
    assert (total, len(postings)) == (2299, 25)
    assert postings[0].url == (
        "https://egup.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/20275435"
    )


def test_oracle_hcm_sends_location_filter():
    finders = []

    def handler(request):
        finders.append(request.url.params["finder"])
        return httpx.Response(200, json={"items": [{"TotalJobsCount": 0, "requisitionList": []}]})

    c = company("oracle_hcm", "https://egup.fa.us2.oraclecloud.com/x", site="CX", location_id="300")
    oracle_hcm.fetch(c, mock_client(handler), delay=0)
    assert "locationId=300" in finders[0] and "siteNumber=CX" in finders[0]


def test_healthcaresource():
    postings, total = healthcaresource.parse(
        json.loads(fixture_text("healthcaresource_anmed.json")), "anmed"
    )
    assert total == 254
    assert postings[0].location == "Anderson, SC"
    assert postings[0].source_id == "19380"
    assert postings[0].url == "https://pm.healthcaresource.com/cs/anmed#/job/24064"


def test_healthcaresource_filters_categories():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"hits": {"total": {"value": 0}, "hits": []}})

    c = company(
        "healthcaresource", "https://pm.healthcaresource.com/cs/anmed", categories=["Support"]
    )
    healthcaresource.fetch(c, mock_client(handler), delay=0)
    assert bodies[0]["query"]["bool"]["filter"] == [
        {"terms": {"occupationalCategory.raw": ["Support"]}}
    ]


def test_healthcaresource_rejects_unexpected_url():
    with pytest.raises(ValueError):
        healthcaresource._site("https://example.com/careers")


def test_bmw():
    postings, total = bmw.parse(
        fixture_text("bmw_upstate.html"), "https://www.bmwgroup.jobs/us/en/x.html"
    )
    assert (total, len(postings)) == (4, 4)
    p = postings[0]
    assert (p.title, p.location, p.posted_date) == (
        "Paint Shop Digitalization Developer Intern",
        "Spartanburg, SC",
        date(2026, 10, 8),
    )
    assert p.url == "https://www.bmwgroup.jobs/us/en/jobfinder/job-description-copy.192851.html"


def test_phenom_title_only_drops_full_text_matches():
    jobs = [
        {"title": "Registered Nurse - Data Entry Support", "jobSeqNo": "1"},
        {"title": "Systems Analyst II", "jobSeqNo": "2"},
        {"title": "Respiratory Care Practitioner", "jobSeqNo": "3"},  # "data" only in its text
    ]

    def handler(request):
        return httpx.Response(200, json={"refineSearch": {"totalHits": 3, "data": {"jobs": jobs}}})

    c = company(
        "phenom", "https://careers.bonsecours.com/x", keywords=["analyst", "data"], title_only=True
    )
    titles = sorted(p.title for p in phenom.fetch(c, mock_client(handler), delay=0))
    assert titles == ["Registered Nurse - Data Entry Support", "Systems Analyst II"]
