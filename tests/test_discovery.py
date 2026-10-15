import json
from datetime import date

import httpx
import pytest
from conftest import fixture_text, mock_client

from jobscraper.adapters import generic
from jobscraper.config import Config, Notifications, Profile
from jobscraper.models import Company
from jobscraper.scraping import crawl, discovery
from jobscraper.storage import company_store


def client_for(pages):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        value = pages[str(request.url)]
        return httpx.Response(
            200, **({"json": value} if isinstance(value, dict | list) else {"text": value})
        )

    return crawl.Client(mock_client(handler), validate=lambda url: None, sleep=lambda delay: None)


@pytest.mark.parametrize(
    ("url", "fixture", "strategy"),
    [
        ("https://acme.bamboohr.com/careers", None, "bamboohr"),
        (
            "https://recruiting.paylocity.com/recruiting/jobs/All/abc",
            "paylocity_keymark.html",
            "paylocity",
        ),
        ("https://recruiting.ultipro.com/ABC/JobBoard/123", None, "ultipro"),
        ("https://careers.arthrex.com/go/SC/7566200/", "arthrex.html", "successfactors"),
        (
            "https://egup.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX",
            None,
            "oracle_hcm",
        ),
        ("https://pm.healthcaresource.com/cs/anmed", None, "healthcaresource"),
        ("https://jobs.michelinman.com/search", "michelin_p1.html", "michelin"),
        (
            "https://moveupstatesc.com/find-a-job/it-tech-development/",
            "moveupstatesc_p1.html",
            "moveupstatesc",
        ),
        ("https://careers.prismahealth.org/jobs/", "prisma_p1.html", "prisma"),
        (
            "https://www.bmwgroup.jobs/jobfinder30.jobfinder_table.content.html",
            "bmw_upstate.html",
            "bmw",
        ),
    ],
)
def test_platform_evidence(url, fixture, strategy):
    assert discovery.identify(url, fixture_text(fixture) if fixture else "")[0] == strategy


def test_phenom_fingerprint():
    assert (
        discovery.identify(
            "https://careers.example.com", "<script>var phApp = {}; /* phenom */</script>"
        )[0]
        == "phenom"
    )
    assert discovery.identify("https://example.com", "We work with Phenom") is None


def test_follows_employer_link_to_bamboohr():
    site = "https://example.com/careers"
    board = "https://acme.bamboohr.com/careers"
    client = client_for(
        {
            site: f'<title>Acme Careers</title><a href="{board}">View jobs</a>',
            board: "<title>Acme careers</title>",
            "https://acme.bamboohr.com/careers/list": json.loads(
                fixture_text("bamboohr_kopis.json")
            ),
        }
    )
    result = discovery.inspect(site, client)
    assert result.company.adapter == "auto"
    assert result.company.options["strategy"] == "bamboohr"
    assert result.company.url == board
    assert result.postings


def test_unfamiliar_json_feed_infers_mapping():
    url = "https://unknown.example/jobs.json"
    client = client_for(
        {
            url: {
                "data": {
                    "jobs": [
                        {
                            "id": "123",
                            "title": "Software Developer",
                            "url": "/jobs/123",
                            "location": "Greenville, SC",
                        }
                    ]
                },
                "total": 1,
            }
        }
    )
    result = discovery.inspect(url, client, name="Unknown Co")
    assert result.company.options["strategy"] == "generic"
    assert result.company.options["settings"]["items"] == "data.jobs"
    assert result.postings[0].source_id == "123"
    assert result.postings[0].url == "https://unknown.example/jobs/123"


def test_unfamiliar_structured_page():
    url = "https://unknown.example/careers"
    job = {
        "@type": ["JobPosting"],
        "title": "Developer",
        "identifier": {"value": "req-7"},
        "url": "/jobs/7",
        "datePosted": "2026-10-01",
        "description": "<p>Build software</p>",
        "jobLocation": {"address": {"addressLocality": "Greenville", "addressRegion": "SC"}},
    }
    html = '<script type="application/ld+json">' + json.dumps({"@graph": [job]}) + "</script>"
    result = discovery.inspect(url, client_for({url: html}), name="Unknown Co")
    assert result.company.options["settings"]["format"] == "jsonld"
    assert result.postings[0].source_id == "req-7"
    assert result.postings[0].location == "Greenville, SC"
    assert result.postings[0].summary == "Build software"
    assert result.postings[0].posted_date == date(2026, 10, 1)


def test_browser_only_page_returns_help_without_company():
    url = "https://unknown.example/careers"
    result = discovery.inspect(url, client_for({url: '<div id="app"></div>'}))
    assert result.company is None
    assert any("Network" in tip for tip in result.help)


def test_manual_html_settings_work_without_new_adapter():
    url = "https://unknown.example/careers"
    html = (
        '<div class="opening"><h2>Developer</h2>'
        '<a href="/roles/123">Apply</a><small>SC</small></div>'
    )
    result = discovery.inspect(
        url,
        client_for({url: html}),
        settings={
            "format": "html",
            "item": ".opening",
            "fields": {"title": "h2", "url": "a@href", "location": "small"},
        },
    )
    assert result.company is not None
    assert result.postings[0].title == "Developer"
    assert result.postings[0].location == "SC"


def test_generic_json_rejects_truncated_results():
    url = "https://unknown.example/feed"
    data = {"jobs": [{"id": "1", "title": "Dev", "url": "/jobs/1"}], "total": 20}
    with pytest.raises(ValueError, match="more jobs"):
        discovery.inspect(url, client_for({url: data}))


def test_generic_follows_next_and_preserves_ids():
    url = "https://unknown.example/jobs"
    html = '<a class="job" href="/jobs/1">Developer</a><a rel="next" href="?page=2">Next</a>'
    client = client_for({url: html, url + "?page=2": '<a class="job" href="/jobs/2">Analyst</a>'})
    result = discovery.inspect(url, client)
    assert len(result.postings) == 2
    assert result.postings[0].source_id != result.postings[1].source_id


def test_generic_rejects_repeated_pagination():
    url = "https://unknown.example/jobs"
    html = '<a class="job" href="/jobs/1">Developer</a><a rel="next" href="/jobs">Next</a>'
    with pytest.raises(ValueError, match="repeated a page"):
        discovery.inspect(url, client_for({url: html}))


def test_store_merges_without_modifying_config_or_marks(tmp_path):
    cfg = Config(Profile("dev"), Notifications("test@example.com"))
    path = tmp_path / "companies.json"
    c = Company(
        "new",
        "New",
        "auto",
        "https://example.com",
        date(2026, 10, 14),
        options={"strategy": "generic"},
    )
    company_store.add(path, c)
    assert company_store.merge(cfg, path).companies == [c]
    assert cfg.companies == []
    with pytest.raises(ValueError, match="already"):
        company_store.add(path, c)
    with pytest.raises(ValueError, match="both"):
        company_store.merge(Config(cfg.profile, cfg.notifications, [c]), path)


def test_changed_html_is_failure_not_empty_success():
    with pytest.raises(ValueError, match="page may have changed"):
        generic.parse_html(
            "<p>Please sign in</p>",
            "https://example.com",
            {
                "format": "html",
                "item": ".job",
                "fields": {"title": "", "url": "@href"},
            },
        )


def test_manual_json_post_with_offset_pages():
    seen = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        assert request.method == "POST"
        body = json.loads(request.content)
        seen.append(body)
        offset = body["offset"]
        return httpx.Response(
            200,
            json={
                "jobs": [{"id": str(offset), "title": "Developer", "url": f"/jobs/{offset}"}],
                "total": 2,
            },
        )

    client = crawl.Client(mock_client(handler), validate=lambda url: None, sleep=lambda delay: None)
    result = discovery.inspect(
        "https://unknown.example/search",
        client,
        settings={
            "format": "json",
            "method": "POST",
            "body": {"limit": 1, "searchText": "dev"},
            "items": "jobs",
            "fields": {"source_id": "id", "title": "title", "url": "url"},
            "total": "total",
            "page_field": "offset",
            "page_start": 0,
            "page_step": 1,
        },
    )
    assert [p.source_id for p in result.postings] == ["0", "1"]
    assert [b["offset"] for b in seen] == [0, 1]
    assert all(b["searchText"] == "dev" for b in seen)


def test_manual_confirmed_empty_page_can_be_tracked():
    url = "https://unknown.example/careers"
    result = discovery.inspect(
        url,
        client_for({url: "<p>No open positions</p>"}),
        settings={
            "format": "html",
            "item": ".job",
            "fields": {"title": "h2", "url": "a@href"},
            "none_text": "No open positions",
        },
    )
    assert result.company is not None and result.postings == []


def test_generic_reuses_existing_html_fixture():
    result = discovery.inspect(
        "https://www.robojuice.com/careers",
        client_for(
            {
                "https://www.robojuice.com/careers": fixture_text("robojuice.html"),
            }
        ),
    )
    assert result.company is not None
    assert result.postings[0].title == "Senior Full-stack PHP Developer"


@pytest.mark.parametrize(
    "settings",
    [
        {"format": "html", "item": "[", "fields": {"title": "", "url": "@href"}},
        {"format": "json", "fields": {"title": [], "url": "url"}},
        {"format": "json", "fields": {"title": "title", "url": "url"}, "method": "DELETE"},
        {"format": "json", "fields": {"title": "title", "url": "url"}, "page_field": "offset"},
    ],
)
def test_invalid_manual_settings_fail_before_network(settings):
    with pytest.raises(ValueError):
        discovery.inspect("https://unknown.example", client_for({}), settings=settings)


def test_html_without_individual_links_uses_explicit_title_ids():
    url = "https://www.externalsoftware.com/contract-opportunities/"
    result = discovery.inspect(
        url,
        client_for({url: fixture_text("externalsoftware.html")}),
        settings={
            "format": "html",
            "item": "h3",
            "fields": {"title": "$text", "url": "$page", "source_id": "$text"},
        },
    )
    assert result.postings[0].title == "Front-end Developer"
    assert result.postings[0].url == url


def test_discovery_follows_unknown_external_board_and_accessible_label():
    home, board = "https://employer.example/hr", "https://board.example/careers/acme"
    result = discovery.inspect(
        home,
        client_for(
            {
                home: (
                    f'<a href="{board}" aria-label="Employment opportunities">'
                    '<img alt="Apply now"></a>'
                ),
                board: '<a class="job" href="/jobs/123">Software Developer</a>',
            }
        ),
    )
    assert result.company.url == board
    assert len(result.postings) == 1


def test_discovery_tries_second_candidate_after_failure():
    home, board = "https://employer.example/hr", "https://board.example/careers"

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if str(request.url) == home:
            return httpx.Response(
                200,
                text=f'<a href="/jobs/broken">Job openings</a>'
                f'<a href="{board}">Employment opportunities</a>',
            )
        if str(request.url) == board:
            return httpx.Response(200, text='<a class="job" href="/jobs/123">Developer</a>')
        return httpx.Response(404)

    client = crawl.Client(mock_client(handler), validate=lambda url: None, sleep=lambda d: None)
    result = discovery.inspect(home, client)
    assert result.company.url == board


def test_discovery_follows_get_search_form():
    home, search = "https://jobs.example/", "https://jobs.example/search/?q=&locationsearch=SC"
    result = discovery.inspect(
        home,
        client_for(
            {
                home: '<form action="/search/"><input name="q">'
                '<input name="locationsearch" value="SC">'
                "<button>Search Jobs</button></form>",
                search: '<a class="job" href="/job/developer/123">Developer</a>',
            }
        ),
    )
    assert result.company.url == search


def test_career_navigation_excludes_documents_and_post_forms():
    links = discovery.career_links(
        """<a href="/jobs.pdf">Job openings</a>
      <form method="post" action="/jobs"><button>Search Jobs</button></form>
      <a href="https://other.example/careers">Current vacancies</a>""",
        "https://example.com/hr",
    )
    assert links == ["https://other.example/careers"]


def test_search_result_table_preserves_location_and_pages():
    url = "https://jobs.example/search/"
    first = (
        '<table><tr class="data-row"><td><a class="jobTitle-link" href="/job/Dev/1">'
        'Developer</a></td><td class="jobLocation">Greenville, SC</td></tr></table>'
        '<ul class="pagination"><li class="active"><a href="/search/">1</a></li>'
        '<li><a href="/search/?startrow=1">2</a></li></ul>'
    )
    second = (
        '<table><tr class="data-row"><td><a class="jobTitle-link" href="/job/Dev/2">'
        'Developer II</a></td><td class="jobLocation">Charlotte, NC</td></tr></table>'
        '<ul class="pagination"><li class="active">2</li><li class="disabled">'
        '<a href="#" aria-label="Next Page">Next</a></li></ul>'
    )
    result = discovery.inspect(url, client_for({url: first, url + "?startrow=1": second}))
    assert [p.location for p in result.postings] == ["Greenville, SC", "Charlotte, NC"]
    assert len(result.postings) == 2


def test_anderson_inline_cards_are_found_before_external_navigation():
    url = "https://www.andersoncountysc.org/work-live/for-residents/human-resources/"
    html = (
        fixture_text("anderson_jobs.html") + '<a href="https://sheriff.example/employment">Jobs</a>'
    )
    result = discovery.inspect(url, client_for({url: html}))
    assert result.company.url == url
    assert len(result.postings) == 14
    assert result.postings[0].title == "Truck Driver Class A"
    assert all(p.url == url for p in result.postings)
    assert len({p.source_id for p in result.postings}) == 14
    assert "Roads & Bridges" in result.postings[0].summary


def test_inline_card_ids_do_not_depend_on_order_or_modal_number():
    from bs4 import BeautifulSoup

    url = "https://county.example/hr"
    html = fixture_text("anderson_jobs.html")
    recipe = discovery.infer_html(html, url)
    first, _ = generic.parse_html(html, url, recipe)
    soup = BeautifulSoup(html, "html.parser")
    for card in soup.select(".job-tile"):
        card.extract()
        soup.insert(0, card)
    second, _ = generic.parse_html(str(soup).replace("jobCenter-", "renumbered-"), url, recipe)
    assert {p.title: p.source_id for p in first} == {p.title: p.source_id for p in second}


def test_zf_last_page_does_not_follow_last_control():
    url = "https://jobs.zf.com/search/?q=&sortColumn=referencedate&sortDirection=desc&startrow=125"
    result = discovery.inspect(url, client_for({url: fixture_text("zf_last_page.html")}))
    assert result.company is not None
    assert len(result.postings) == 2


def test_generic_search_pages_are_not_career_candidates():
    assert (
        discovery.career_links(
            '<form action="/search"><input name="q"><button>Search</button></form>'
            '<a href="/search?type=pages&page=2">2</a>',
            "https://county.example/hr",
        )
        == []
    )


def test_inline_cards_refuse_ambiguous_ids():
    url = "https://county.example/hr"
    html = fixture_text("anderson_jobs.html").replace("Deputy", "Truck Driver Class A")
    with pytest.raises(ValueError, match="share an identifier"):
        discovery.inspect(url, client_for({url: html}))
