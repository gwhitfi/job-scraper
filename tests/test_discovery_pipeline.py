import json
from datetime import date

import httpx
from conftest import fixture_text, mock_client
from test_discovery import client_for

from jobscraper.scraping import crawl, discovery, navigation


def test_new_results_candidate_outranks_old_marketing_queue():
    home = "https://example.com/careers"
    calls = []
    pages = {
        home: '<a href="/careers/engineering">Careers</a><a href="/careers/benefits">Careers</a>',
        "https://example.com/careers/engineering": '<a href="/results">Search jobs</a>',
        "https://example.com/results": '<a class="job" href="/jobs/1">Developer</a>',
    }

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls.append(str(request.url))
        return httpx.Response(200, text=pages[str(request.url)])

    client = crawl.Client(mock_client(handler), validate=lambda u: None, sleep=lambda d: None)
    result = discovery.inspect(home, client)
    assert result.company.url == "https://example.com/results"
    assert calls == [home, home + "/engineering", "https://example.com/results"]


def test_failed_extraction_tries_another_candidate():
    home = "https://example.com/careers"
    result = discovery.inspect(
        home,
        client_for(
            {
                home: '<a href="/results">Search jobs</a><a href="/careers/openings">View jobs</a>',
                "https://example.com/results": (
                    '<a class="job" href="/jobs/1">Developer</a>'
                    '<a rel="next" href="/results">Next</a>'
                ),
                "https://example.com/careers/openings": (
                    '<a class="job" href="/jobs/2">Engineer</a>'
                ),
            }
        ),
    )
    assert result.company.url.endswith("/careers/openings")
    assert result.postings[0].title == "Engineer"


def test_target_navigation_uses_public_board_not_login():
    candidates = discovery.career_links(
        fixture_text("target_navigation.html")
        + '<a href="https://target.wd5.myworkdayjobs.com/targetcareers/login?redirect=x">'
        "Sign in</a>",
        "https://corporate.target.com/careers",
    )
    assert "https://corporate.target.com/careers/job-search" in candidates
    assert "https://target.wd5.myworkdayjobs.com/targetcareers" in candidates
    assert all(
        "/login" not in u and "/job-alerts" not in u and "/faqs" not in u for u in candidates
    )
    assert candidates.index("https://corporate.target.com/careers/job-search") < candidates.index(
        "https://target.wd5.myworkdayjobs.com/targetcareers"
    )


def test_canonical_urls_deduplicate_tracking_and_query_order():
    assert navigation.canonical(
        "https://example.com/jobs?b=2&a=1&utm_source=x#top"
    ) == navigation.canonical("https://example.com/jobs?a=1&b=2")


def test_security_challenge_is_blocked_not_missing_selectors():
    url = "https://selfservice.example/jobs"
    result = discovery.inspect(url, client_for({url: fixture_text("greenville_challenge.html")}))
    assert result.company is None and result.status == "blocked"
    assert "security challenge" in result.message


def test_robots_denial_is_explicit():
    client = crawl.Client(
        mock_client(lambda r: httpx.Response(200, text="User-agent: *\nDisallow: /")),
        validate=lambda u: None,
    )
    result = discovery.inspect("https://example.com/careers", client)
    assert result.status == "blocked"
    assert "Disallow: /" in result.message


def test_embedded_json_jobs_can_be_discovered_and_recrawled():
    url = "https://example.com/careers"
    data = {
        "props": {"jobs": [{"id": "1", "title": "Developer", "url": "/jobs/1", "location": "SC"}]}
    }
    result = discovery.inspect(
        url,
        client_for(
            {
                url: '<script type="application/json" id="__NEXT_DATA__">'
                + json.dumps(data)
                + "</script>"
            }
        ),
    )
    assert result.company.options["settings"]["script_id"] == "__NEXT_DATA__"
    assert result.postings[0].source_id == "1"


def test_wordpress_job_manager_live_fixture_discovers_public_feed():
    url = "https://www.cityofandersonsc.com/careers/"
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/careers/":
            return httpx.Response(200, text=fixture_text("wp_job_manager_shell.html"))
        assert request.url.path == "/jm-ajax/get_listings/"
        assert request.method == "POST"
        assert b"page=1" in request.content
        return httpx.Response(200, json=json.loads(fixture_text("wp_job_manager_listings.json")))

    client = crawl.Client(mock_client(handler), validate=lambda u: None, sleep=lambda d: None)
    result = discovery.inspect(url, client)
    assert result.company.options["strategy"] == "wp_job_manager"
    assert len(result.postings) == 7
    assert result.postings[0].source_id == "14179"
    assert result.postings[0].posted_date == date(2026, 10, 12)
    assert len([r for r in requests if r.url.path == "/careers/"]) == 1


def test_wordpress_job_manager_ignores_woocommerce_ajax_endpoint():
    url = "https://flexonhose.com/careers/"
    shell = (
        "<script>var wc_add_to_cart_params = "
        '{"ajax_url":"/wp-admin/admin-ajax.php"};</script>'
        + fixture_text("wp_job_manager_shell.html")
    )

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path == "/careers/":
            return httpx.Response(200, text=shell)
        assert request.url.path == "/jm-ajax/get_listings/"
        assert request.method == "POST"
        return httpx.Response(200, json=json.loads(fixture_text("wp_job_manager_flexon.json")))

    client = crawl.Client(mock_client(handler), validate=lambda u: None, sleep=lambda d: None)
    result = discovery.inspect(url, client)
    assert result.status == "added"
    assert len(result.postings) == 6
    assert sum(p.location == "Anderson, SC" for p in result.postings) == 3


def test_generic_empty_browser_shell_does_not_become_false_empty_success():
    url = "https://example.com/careers"
    result = discovery.inspect(
        url, client_for({url: '<div id="app">Loading jobs</div><p>No results</p>'})
    )
    assert result.company is None


def test_walmart_video_carousel_is_not_a_job_list():
    data = json.loads(fixture_text("walmart_videos.json"))
    assert discovery.infer_json(data) is None
    html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(data) + "</script>"
    assert discovery.embedded_recipe(html) is None


def test_nested_json_count_is_preserved():
    recipe = discovery.infer_json(
        {"props": {"jobs": [{"title": "Developer", "url": "/jobs/1"}], "total": 12}}
    )
    assert recipe["total"] == "props.total"
