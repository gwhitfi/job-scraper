import json
import os
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from jobscraper.classification import jev_triage
from jobscraper.config import Config, Notifications, Profile
from jobscraper.models import Company
from jobscraper.presentation import web
from jobscraper.storage import jsonfile, state
from jobscraper.storage.state import Health, Posting, State

NOW = datetime.now(UTC)
PROFILE = Profile(target_role="dev", levels=("entry", "mid"))


def posting(
    key, title, relevance="core", level="mid", status="active", employer="", check="ok", **kw
):
    cid, sid = key.split(":")
    return Posting(
        key=key,
        company_id=cid,
        source_id=sid,
        title=title,
        location="Greenville, SC",
        url=f"https://x/{sid}",
        posted_date=kw.pop("posted_date", None),
        first_seen=kw.pop("first_seen", NOW - timedelta(days=10)),
        last_seen=NOW,
        status=status,
        employer=employer,
        classification={
            "relevance": relevance,
            "level": level,
            "reason": f"{title} reason",
            "employer_check": check,
            "employer_note": "unknown staffing firm" if check == "verify" else "",
        },
        **kw,
    )


@pytest.fixture
def app(tmp_path):
    companies = [
        Company(id="acme", name="Acme", adapter="ultipro", url="u", added=date(2026, 1, 1)),
        Company(
            id="mus",
            name="Move Upstate SC",
            adapter="moveupstatesc",
            url="u",
            added=date(2026, 1, 1),
        ),
    ]
    cfg = Config(PROFILE, Notifications("a@b.c"), companies)
    st = State(
        postings={
            p.key: p
            for p in [
                posting("acme:1", "Web Developer", first_seen=NOW),
                posting("acme:2", "Data Analyst", relevance="adjacent"),
                posting("acme:3", "Ops Specialist", relevance="maybe"),
                posting("acme:4", "Forklift Operator", relevance="none"),
                posting("acme:5", "Senior Developer", level="senior"),
                posting("acme:6", "Old Developer", status="removed"),
                posting("mus:9", "BI Developer", employer="AFL", posted_date=date(2026, 10, 1)),
                posting("mus:10", "Remote Developer", employer="Shady LLC", check="verify"),
                posting("mus:11", "Junior Developer", employer="Shady LLC", check="verify"),
            ]
        },
        health={
            "acme": Health(last_success=NOW),
            "mus": Health(consecutive_failures=3, last_error="403"),
        },
    )
    state.save(st, tmp_path / "state")
    return web.App(cfg, tmp_path / "state", tmp_path / "marks.json", tmp_path / "employers.json")


def keys(app, view, source="all", q=""):
    rows = app.rows()
    return sorted(
        r.posting.key
        for r in rows
        if app.in_view(r, view) and app.in_source(r, source) and app.matches_query(r, q)
    )


def test_views(app):
    assert keys(app, "exact") == ["acme:1", "mus:9"]
    assert keys(app, "near") == ["acme:2"]
    assert keys(app, "maybe") == ["acme:3"]
    assert keys(app, "relevant") == ["acme:1", "acme:2", "acme:3", "mus:9"]


def test_sources_and_search(app):
    assert keys(app, "relevant", "boards") == ["mus:9"]
    assert keys(app, "relevant", "companies") == ["acme:1", "acme:2", "acme:3"]
    assert keys(app, "relevant", q="afl") == ["mus:9"]
    assert keys(app, "relevant", q="data greenville") == ["acme:2"]


def test_marks_move_postings_between_views(app):
    app.mark("acme:1", "hidden", True)
    app.mark("acme:2", "starred", True)
    app.mark("acme:6", "applied", True)  # closed job stays visible under Applied
    assert "acme:1" not in keys(app, "exact")
    assert keys(app, "hidden") == ["acme:1"]
    assert keys(app, "starred") == ["acme:2"]
    assert keys(app, "applied") == ["acme:6"]


def test_starred_jobs_leave_match_tabs(app):
    app.mark("acme:2", "starred", True)
    assert "acme:2" not in keys(app, "near") + keys(app, "relevant")
    assert keys(app, "starred") == ["acme:2"]
    app.mark("acme:2", "starred", False)
    assert keys(app, "near") == ["acme:2"]


def test_mark_unknown_posting(app):
    with pytest.raises(KeyError):
        app.mark("acme:999", "starred", True)


def test_page_renders(app):
    html = app.index({"view": "exact"})
    assert "Web Developer" in html
    assert "AFL (via Move Upstate SC)" in html
    assert "Data Analyst" not in html
    assert "Site checks failing: Move Upstate SC" in html
    assert 'class="badge new latest"' in html  # acme:1 found by the latest check
    assert "posted Oct 1" in html


def test_page_escapes_user_text(app):
    app.mark("acme:1", "note", "<script>x</script>")
    html = app.index({"view": "exact"})
    assert "&lt;script&gt;x&lt;/script&gt;" in html
    assert "<script>x</script>" not in html


def test_reloads_when_state_changes(app, tmp_path):
    assert "acme:1" in app.state().postings
    st = app.state()
    st.postings.pop("acme:1")
    state.save(st, tmp_path / "state")

    path = tmp_path / "state" / "postings.json"
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
    assert "acme:1" not in app.state().postings


@pytest.fixture
def server(app):
    srv = web.make_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()
    thread.join()


def test_http_get_and_mark(server, app):
    assert httpx.get(server + "/?view=near").status_code == 200
    assert httpx.get(server + "/nope").status_code == 404

    body = json.dumps({"key": "acme:2", "field": "starred", "value": True})
    headers = {"Content-Type": "application/json", "X-Requested-With": "jobscraper"}
    resp = httpx.post(server + "/api/mark", content=body, headers=headers)
    assert resp.status_code == 200 and resp.json()["starred"] is True
    assert keys(app, "starred") == ["acme:2"]


def test_http_mark_rejects_cross_site_and_bad_input(server):
    body = json.dumps({"key": "acme:2", "field": "starred", "value": True})
    assert httpx.post(server + "/api/mark", content=body).status_code == 403
    headers = {"X-Requested-With": "jobscraper"}
    bad = json.dumps({"key": "acme:2", "field": "starred", "value": "yes"})
    assert httpx.post(server + "/api/mark", content=bad, headers=headers).status_code == 400
    unknown = json.dumps({"key": "zz:1", "field": "starred", "value": True})
    assert httpx.post(server + "/api/mark", content=unknown, headers=headers).status_code == 404


def test_settings_panel_lists_fonts(app):
    html = app.index({})
    assert 'id="settings"' in html
    assert html.count('<select data-slot="head">') == 1
    assert html.count('<select data-slot="body">') == 1
    assert '<option value="JetBrains Mono" data-cat="mono">' in html
    assert '<option value="Poppins" data-cat="sans">' in html


def test_settings_buttons_are_not_mark_toggles(app):
    html = app.index({})
    dialog = html[html.index('<dialog id="settings">') : html.index("</dialog>")]
    assert "toggle" not in dialog
    assert 'closest(".card .toggle")' in html


def test_score_badge_and_relevance_sort(app):
    st = app.state()
    st.postings["acme:1"].classification["score"] = 6
    st.postings["mus:9"].classification["score"] = 10
    html = app.index({"view": "exact", "sort": "relevant"})
    assert html.index("BI Developer") < html.index("Web Developer")
    assert "10/10</span>" in html and "6/10</span>" in html
    assert 'class="card tier-high"' in html and 'class="card tier-mid"' in html
    newest = app.index({"view": "exact", "sort": "newest"})
    assert newest.index("Web Developer") < newest.index("BI Developer")


def test_extended_area_shows_only_exact(app):
    st = app.state()
    st.postings["acme:1"].area = "extended"  # exact
    st.postings["acme:2"].area = "extended"  # near
    assert "acme:1" in keys(app, "exact")
    assert "acme:2" not in keys(app, "near")
    assert "Outside Upstate" in app.index({"view": "exact"})


def test_unverified_employers_have_their_own_tab(app):
    assert keys(app, "unverified") == ["mus:10", "mus:11"]
    assert "mus:10" not in keys(app, "exact")
    html = app.index({"view": "unverified"})
    assert "Unverified employer: unknown staffing firm" in html
    assert 'data-action="approve"' in html and 'data-action="block"' in html
    assert "google.com/search" in html


def test_approve_moves_all_postings_from_employer(app):
    app.decide_employer("employer:shady", "approve")
    assert keys(app, "unverified") == []
    assert {"mus:10", "mus:11"} <= set(keys(app, "exact"))


def test_block_and_unblock(app):
    app.decide_employer("employer:shady", "block")
    assert keys(app, "blocked") == ["mus:10", "mus:11"]
    assert "mus:10" not in keys(app, "exact") and keys(app, "unverified") == []
    assert 'data-action="clear"' in app.index({"view": "blocked"})
    app.decide_employer("employer:shady", "clear")
    assert keys(app, "unverified") == ["mus:10", "mus:11"]


def test_decide_unknown_employer_or_action(app):
    with pytest.raises(KeyError):
        app.decide_employer("employer:nobody", "approve")
    with pytest.raises(ValueError):
        app.decide_employer("employer:shady", "maybe")


def test_http_employer_endpoint(server, app):
    headers = {"X-Requested-With": "jobscraper"}
    body = json.dumps({"employer": "employer:shady", "action": "block"})
    assert httpx.post(server + "/api/employer", content=body, headers=headers).status_code == 200
    assert keys(app, "blocked") == ["mus:10", "mus:11"]
    assert httpx.post(server + "/api/employer", content=body).status_code == 403


@pytest.mark.parametrize(
    "now,expected",
    [
        ("2026-10-15T13:06:00+00:00", "2026-10-15T14:00:00+00:00"),
        ("2026-10-15T14:00:00+00:00", "2026-10-15T16:00:00+00:00"),
        ("2026-10-15T14:00:00.000001+00:00", "2026-10-15T16:00:00+00:00"),
        ("2026-10-15T23:40:00+00:00", "2026-10-16T00:00:00+00:00"),
        ("2026-12-15T14:06:00+00:00", "2026-12-15T15:00:00+00:00"),
        ("2026-11-01T05:30:00+00:00", "2026-11-01T07:00:00+00:00"),
        ("2027-03-14T06:30:00+00:00", "2027-03-14T08:00:00+00:00"),
    ],
)
def test_next_run(now, expected):
    assert web.next_run(datetime.fromisoformat(now)) == datetime.fromisoformat(expected)


def test_schedule_matches_local_timer():
    timer = (Path(__file__).parent.parent / "deploy/jobscraper-crawl.timer").read_text()
    schedule = (
        f"OnCalendar=*-*-* 00/{web.SCHEDULE_EVERY_HOURS}:"
        f"{web.SCHEDULE_MINUTE:02d}:00 America/New_York"
    )
    assert schedule in timer
    assert "AccuracySec=1us" in timer and "RandomizedDelaySec=0" in timer


def test_header_shows_next_update(app):
    assert "· next " in app.index({})


def test_stale_banner(app):
    st = app.state()
    assert "No update in" not in app.index({})
    for h in st.health.values():
        if h.last_success:
            h.last_success = NOW - timedelta(hours=web.STALE_HOURS + 1)
    assert f"No update in {web.STALE_HOURS + 1} hours" in app.index({})


def test_email_failure_banner(app, tmp_path):
    from jobscraper.workflow import notify

    outbox = notify.load_outbox(tmp_path / "state" / "email.json")
    notify.queue(outbox, {"subject": "s", "text": "t", "html": "h"})
    outbox["last_error"] = "SMTPAuthenticationError: 535 bad <creds>"
    jsonfile.write(tmp_path / "state" / "email.json", outbox)
    html = app.index({})
    assert "Alert email failing (1 unsent): SMTPAuthenticationError: 535 bad &lt;creds&gt;" in html


def test_duplicate_hidden_and_linked_from_company_card(app):
    st = app.state()
    st.postings["mus:9"].duplicate_of = "acme:1"
    assert "mus:9" not in keys(app, "exact")
    html = app.index({"view": "exact"})
    assert "Also listed on" in html and ">Move Upstate SC</a>" in html


def test_nearest_sort_and_miles(app):
    st = app.state()
    st.postings["acme:1"].location = "Charlotte, NC"
    st.postings["mus:9"].location = "Pendleton, SC"
    st.postings["acme:3"].location = "Remote"
    html = app.index({"view": "relevant", "sort": "nearest"})
    titles = ("BI Developer", "Data Analyst", "Web Developer", "Ops Specialist")
    order = [html.index(t) for t in titles]
    assert order == sorted(order)  # 11 mi, 27 mi, 113 mi, then remote last
    assert "Pendleton, SC · 11 mi" in html


def test_footer_shows_claude_cost(app, tmp_path):
    from jobscraper.classification import usage

    run = usage.Usage(calls=1, input_tokens=100_000, output_tokens=10_000)
    usage.record(tmp_path / "state" / "usage.json", run, "claude-sonnet-5-5", NOW)
    assert "Claude: $0.30 this month" in app.index({})


def test_page_drops_unsafe_links(app):
    app.state().postings["acme:1"].url = "javascript:alert(document.cookie)"
    html = app.index({"view": "exact"})
    assert "javascript:alert" not in html
    assert '<h2><a href="#"' in html


def test_reloads_config_when_it_changes(tmp_path):
    from jobscraper import config

    base = """
[profile]
target_role = "dev"
levels = ["entry", "mid"]
[notifications]
email = "a@b.c"
[[companies]]
id = "acme"
name = "{name}"
adapter = "ultipro"
url = "u"
added = 2026-01-01
"""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(base.format(name="Acme"))
    state.save(State(postings={"acme:1": posting("acme:1", "Web Developer")}), tmp_path / "state")
    app = web.App(
        config.load(cfg_path),
        tmp_path / "state",
        tmp_path / "marks.json",
        tmp_path / "employers.json",
        cfg_path,
    )
    assert '<p class="company">Acme</p>' in app.index({})

    def rewrite(text: str, bump: int) -> None:
        cfg_path.write_text(text)
        stat = cfg_path.stat()
        os.utime(cfg_path, (stat.st_atime, stat.st_mtime + bump))

    rewrite(base.format(name="Acme Renamed"), 5)
    assert '<p class="company">Acme Renamed</p>' in app.index({})

    rewrite("not = [valid toml", 10)  # a broken edit keeps the last good config
    assert '<p class="company">Acme Renamed</p>' in app.index({})


@pytest.mark.parametrize(
    ("score", "tier"),
    [
        (None, ""),
        (0, ""),
        (1, "low"),
        (3, "low"),
        (4, "mid"),
        (6, "mid"),
        (7, "high"),
        (10, "high"),
    ],
)
def test_score_tier(score, tier):
    assert web.score_tier(score) == tier


@pytest.mark.parametrize(
    ("view", "field", "value", "visible"),
    [
        ("exact", "starred", True, False),
        ("relevant", "applied", True, False),
        ("exact", "hidden", True, False),
        ("starred", "starred", True, True),
        ("starred", "starred", False, False),
        ("applied", "applied", True, True),
        ("hidden", "hidden", True, True),
        ("exact", "note", "follow up", True),
    ],
)
def test_http_marks_return_tab_membership_and_counts(server, app, view, field, value, visible):
    response = httpx.post(
        server + f"/api/mark?view={view}&source=companies&q=web",
        json={"key": "acme:1", "field": field, "value": value},
        headers={"X-Requested-With": "jobscraper"},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["visible"] is visible
    assert result["counts"][view] == int(visible)
    assert result["counts"]["unverified"] == 0  # source/search exclude the board jobs
    assert ("acme:1" in keys(app, view, "companies", "web")) is visible
    # Response metadata must never become part of my saved marks.
    assert "counts" not in json.loads(app.marks_path.read_text())["acme:1"]


def test_http_mark_response_respects_search(server):
    response = httpx.post(
        server + "/api/mark?view=hidden&q=nomatch",
        json={"key": "acme:1", "field": "hidden", "value": True},
        headers={"X-Requested-With": "jobscraper"},
    )
    assert response.status_code == 200
    assert response.json()["visible"] is False
    assert not any(response.json()["counts"].values())


def test_applied_jobs_move_to_applied_tab(app):
    assert "acme:1" in keys(app, "exact")
    app.mark("acme:1", "applied", True)
    app.mark("acme:1", "starred", True)
    assert "acme:1" not in keys(app, "exact") + keys(app, "relevant")
    assert "acme:1" in keys(app, "applied") and "acme:1" in keys(app, "starred")


def test_new_badge_tiers(monkeypatch):
    monkeypatch.setattr(web, "BADGES_SINCE", NOW - timedelta(days=30))
    latest = NOW - timedelta(hours=1)

    def tier(hours_ago, **kw):
        p = posting("acme:1", "Dev", first_seen=NOW - timedelta(hours=hours_ago), **kw)
        return web.new_tier(p, latest, NOW)

    assert tier(1) == "latest"
    assert tier(3) == "recent"
    assert tier(47) == "recent"
    assert tier(50) == "older"
    assert tier(95) == "older"
    assert tier(97) == ""
    assert tier(1, status="removed") == ""


def test_no_new_badge_for_postings_from_before_badges(monkeypatch):
    monkeypatch.setattr(web, "BADGES_SINCE", NOW - timedelta(hours=2))
    old = posting("acme:1", "Dev", first_seen=NOW - timedelta(hours=3))
    assert web.new_tier(old, old.first_seen, NOW) == ""


def test_newest_sorts_by_when_found_not_posted(app):
    st = app.state()
    # The board picked this one up today, days after it was posted.
    st.postings["mus:9"].first_seen = NOW
    st.postings["mus:9"].posted_date = date(2026, 9, 1)
    st.postings["acme:1"].first_seen = NOW - timedelta(days=1)
    st.postings["acme:1"].posted_date = date(2026, 10, 12)
    html = app.index({"view": "exact", "sort": "newest"})
    assert html.index("BI Developer") < html.index("Web Developer")
    from bs4 import BeautifulSoup

    label = BeautifulSoup(html, "html.parser").select_one('article[data-key="mus:9"] .added')
    assert label.get_text(strip=True) == "added just now"
    assert datetime.fromisoformat(label["datetime"]) == NOW
    assert label["data-date"] == f"added {NOW.astimezone(web.EASTERN):%b %-d}"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (-60, "added just now"),
        (0, "added just now"),
        (59, "added just now"),
        (60, "added 1 minute ago"),
        (3599, "added 59 minutes ago"),
        (3600, "added 1 hour ago"),
        (7200, "added 2 hours ago"),
        (86400, "added 24 hours ago"),
        (86401, None),
    ],
)
def test_added_label(seconds, expected):
    first_seen = NOW - timedelta(seconds=seconds)
    if expected is None:
        expected = f"added {first_seen.astimezone(web.EASTERN):%b %-d}"
    assert web.added_label(first_seen, NOW) == expected


def test_add_company_endpoint_persists_and_reloads(server, app, monkeypatch):
    from jobscraper.models import RawPosting
    from jobscraper.scraping import discovery

    c = Company(
        "new-co",
        "New Co",
        "auto",
        "https://new.example/jobs",
        date(2026, 10, 14),
        options={"strategy": "generic", "settings": {"format": "jsonld"}},
    )
    monkeypatch.setattr(
        discovery,
        "inspect",
        lambda *args, **kwargs: discovery.Discovery(
            company=c,
            postings=[RawPosting("1", "Developer", "SC", "https://new.example/jobs/1")],
            message="Found 1 job.",
        ),
    )
    original_config = app.config_path
    headers = {"X-Requested-With": "jobscraper"}
    body = {"url": c.url, "name": c.name, "locations": "SC"}
    assert httpx.post(server + "/api/companies", json=body).status_code == 403
    response = httpx.post(server + "/api/companies", json=body, headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "added"
    assert app.cfg.company(c.id).locations == ("SC",)
    assert app.config_path == original_config
    assert not app.marks_path.exists() and not app.employers_path.exists()
    response = httpx.post(server + "/api/companies", json=body, headers=headers)
    assert response.status_code == 400
    assert "already" in response.text
    reloaded = web.App(app._base_cfg, app.state_dir, app.marks_path, app.employers_path)
    assert reloaded.cfg.company(c.id).name == "New Co"


def test_add_company_missing_info_does_not_enable_tracking(server, app, monkeypatch):
    from jobscraper.scraping import discovery

    monkeypatch.setattr(
        discovery,
        "inspect",
        lambda *args, **kwargs: discovery.Discovery(
            message="Need the results page",
            help=["Click Search jobs"],
        ),
    )
    response = httpx.post(
        server + "/api/companies",
        json={"url": "https://new.example"},
        headers={"X-Requested-With": "jobscraper"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "needs_input"
    assert not app.companies_path.exists()


def test_add_company_form(app):
    html = app.index({})
    assert 'id="open-company"' in html
    assert 'id="company-form"' in html
    assert 'id="company-advanced"' in html


def test_add_company_discovers_then_runner_uses_saved_recipe(app, monkeypatch):
    from conftest import mock_client

    from jobscraper.scraping import crawl
    from jobscraper.storage.state import State
    from jobscraper.workflow import runner

    page = '<title>New Co Careers</title><a class="job" href="/jobs/123">Software Developer</a>'
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(
            200, text="User-agent: *\nAllow: /" if request.url.path == "/robots.txt" else page
        )

    monkeypatch.setattr(crawl, "public_host", lambda url: None)
    monkeypatch.setattr(crawl.time, "sleep", lambda delay: None)
    monkeypatch.setattr(web.http, "client", lambda: mock_client(handler))
    added = app.add_company({"url": "new.example/careers", "name": "New Co", "locations": "SC"})
    assert added["status"] == "added"
    company = app.cfg.company(added["company_id"])
    assert company.adapter == "auto" and company.options["strategy"] == "generic"
    cfg = Config(Profile("software developer"), Notifications("test@example.com"), [company])
    with mock_client(handler) as client:
        result = runner.run(cfg, State(), client, llm=None, sleep=lambda delay: None)
    assert result.failed == {}
    assert result.fetched[company.id] == 0  # No location evidence in the discovered page.
    assert result.digest.new_exact == []
    assert all("new.example" in url for url in requests)


def test_failed_add_company_has_reviewable_log(server, app, monkeypatch):
    from jobscraper.scraping import discovery
    from jobscraper.storage import jsonfile

    def fail(*args, **kwargs):
        raise ValueError("Extraction failed <script>oops()</script>")

    monkeypatch.setattr(discovery, "inspect", fail)
    response = httpx.post(
        server + "/api/companies",
        json={"url": "new.example"},
        headers={"X-Requested-With": "jobscraper"},
    )
    assert response.status_code == 400
    assert "Log: /logs?id=" in response.text
    record = jsonfile.read(app.marks_path.parent / "discovery-history.json")["runs"][0]
    assert record["status"] == "failed"
    assert not app.companies_path.exists()
    page = httpx.get(server + "/logs?id=" + record["id"])
    assert page.status_code == 200
    assert "Extraction failed" in page.text and "<script>oops()" not in page.text
    assert " open>" in page.text


def test_tracked_companies_route_includes_website_additions(server, app):
    from jobscraper.storage import company_store

    company_store.add(
        app.companies_path,
        Company(
            "new-co",
            "New Co",
            "auto",
            "https://new.example/jobs",
            date(2026, 10, 14),
            options={"strategy": "generic"},
        ),
    )
    response = httpx.get(server + "/companies")
    assert response.status_code == 200
    assert "Acme" in response.text and "Move Upstate SC" in response.text
    assert "New Co" in response.text and "Waiting for first crawl" in response.text
    assert "Failing to crawl" in response.text and "Good" in response.text
    assert 'href="/companies"' in app.index({})


def test_health_only_update_invalidates_web_cache(app):
    assert app.state().health["acme"].consecutive_failures == 0
    path = app.state_dir / "health.json"
    data = jsonfile.read(path)
    data["acme"]["consecutive_failures"] = 3
    data["acme"]["last_error"] = "HTTP 403"
    jsonfile.write(path, data)
    assert app.state().health["acme"].consecutive_failures == 3


def test_saved_foreign_jobs_are_excluded_without_deleting_history(app):
    from dataclasses import replace

    app._use_config(
        replace(app.cfg, companies=[replace(c, locations=("SC",)) for c in app.cfg.companies])
    )
    st = app.state()
    for key in ("acme:1", "acme:2", "acme:3", "mus:10"):
        st.postings[key].countries = ("Poland",)
        st.postings[key].location = "Warsaw, Masovian"
    st.postings["acme:1"].title = "Java Developer - Remote in Poland"
    state.save(st, app.state_dir)
    assert keys(app, "exact") == ["mus:9"]
    assert keys(app, "near") == []
    assert keys(app, "maybe") == []
    assert keys(app, "unverified") == ["mus:11"]
    app.mark("acme:1", "starred", True)
    app.mark("acme:2", "applied", True)
    assert keys(app, "starred") == ["acme:1"]
    assert keys(app, "applied") == ["acme:2"]
    assert app.state().postings["acme:1"].status == "active"


def test_current_scope_rechecks_saved_extended_and_unknown_locations(app):
    from dataclasses import replace

    app._use_config(
        replace(
            app.cfg,
            companies=[replace(c, locations=("SC",)) for c in app.cfg.companies],
            extended_locations=("NC",),
        )
    )
    st = app.state()
    st.postings["acme:1"].location = "Charlotte, North Carolina"
    st.postings["acme:2"].location = "Charlotte, North Carolina"
    st.postings["acme:3"].location = ""
    state.save(st, app.state_dir)
    assert "acme:1" in keys(app, "exact")
    assert "acme:2" not in keys(app, "near")
    assert "acme:3" not in keys(app, "maybe")


@pytest.mark.parametrize("location", ["Warsaw, Poland", "Remote - Canada", "", "3 Locations"])
def test_saved_jobs_without_country_metadata_require_location_evidence(app, location):
    from dataclasses import replace

    app._use_config(
        replace(app.cfg, companies=[replace(c, locations=("SC",)) for c in app.cfg.companies])
    )
    st = app.state()
    st.postings["acme:1"].location = location
    st.postings["acme:1"].countries = ()
    st.postings["acme:1"].title = "Remote Developer"
    state.save(st, app.state_dir)
    assert "acme:1" not in keys(app, "exact")
    assert "acme:1" not in keys(app, "relevant")


@pytest.mark.parametrize(
    "query,expected",
    [
        ("!developer", ["acme:2", "acme:3"]),
        ("developer !web", ["mus:9"]),
        ('!"web developer"', ["acme:2", "acme:3", "mus:9"]),
        ('"data analyst" !desktop', ["acme:2"]),
        ("!developer !ops", ["acme:2"]),
    ],
)
def test_exclusion_search(app, query, expected):
    assert keys(app, "relevant", q=query) == expected


def test_salary_sort_annualizes_hourly_and_keeps_unknown_last(app):
    from jobscraper.classification import salaries

    rows = app.rows()[:3]
    for row, amount, period in zip(rows, [50000, 30, None], ["year", "hour", "year"], strict=True):
        if amount:
            row.posting.salary = salaries.serialize(
                salaries.Salary(
                    minimum=amount,
                    maximum=amount * 2,
                    period=period,
                    currency="USD",
                    confidence="low",
                ),
                "x",
            )
    assert [r.posting.key for r in sorted(rows, key=lambda r: app.sort_key(r, "salary"))] == [
        "acme:2",
        "acme:1",
        "acme:3",
    ]
    assert [
        r.posting.key for r in sorted(rows, key=lambda r: app.sort_key(r, "salary_lowest"))
    ] == ["acme:1", "acme:2", "acme:3"]


def test_reverse_distance_and_relevance_keep_missing_last(app):
    rows = app.rows()[:3]
    for row, miles, score in zip(rows, [None, 10, 50], [None, 3, 9], strict=True):
        row.miles = miles
        row.posting.classification["score"] = score
    assert sorted(rows, key=lambda r: app.sort_key(r, "farthest")) == [rows[2], rows[1], rows[0]]
    assert sorted(rows, key=lambda r: app.sort_key(r, "least_relevant")) == [
        rows[1],
        rows[2],
        rows[0],
    ]
    assert sorted(rows, key=lambda r: app.sort_key(r, "oldest"))[-1] == rows[0]


def test_personal_classification_override_persists_and_can_reset(app):
    original = app.state().postings["acme:1"].classification.copy()
    app.mark("acme:1", "group", "maybe")
    assert "acme:1" not in keys(app, "exact")
    assert "acme:1" in keys(app, "maybe")
    reloaded = web.App(app.cfg, app.state_dir, app.marks_path, app.employers_path)
    assert "acme:1" in keys(reloaded, "maybe")
    assert reloaded.state().postings["acme:1"].classification == original
    reloaded.mark("acme:1", "group", "")
    assert "acme:1" in keys(reloaded, "exact")
    with pytest.raises(ValueError):
        app.mark("acme:1", "group", "bogus")


def test_personal_override_cannot_bypass_geography(app):
    from dataclasses import replace

    app._use_config(
        replace(app.cfg, companies=[replace(c, locations=("SC",)) for c in app.cfg.companies])
    )
    app._base_cfg = app.cfg
    app.state().postings["acme:1"].location = "Warsaw, Poland"
    state.save(app.state(), app.state_dir)
    app.mark("acme:1", "group", "exact")
    assert "acme:1" not in keys(app, "exact")
    rows, _ = app.select({"view": "companies", "company": "acme"})
    assert all(r.posting.key != "acme:1" for r in rows)


def test_company_directory_counts_and_condensed_details(app):
    from bs4 import BeautifulSoup

    app.mark("acme:1", "starred", True)
    app.mark("acme:6", "applied", True)
    soup = BeautifulSoup(app.index({"view": "companies"}), "html.parser")
    cards = soup.select(".company-card")
    assert len(cards) == 2
    assert [s.text for s in cards[0].select("strong")] == ["3", "1", "1"]
    assert "company=acme" in cards[0]["href"]
    detail = BeautifulSoup(app.index({"view": "companies", "company": "acme"}), "html.parser")
    assert len(detail.select(".compact-jobs .card")) == 3
    assert "Forklift Operator" not in detail.text
    assert "Old Developer" not in detail.select_one(".compact-jobs").text
    assert not detail.select(".reclassify")
    assert len(detail.select(".card-check")) == 3
    assert detail.select_one('input[name="company"]')["value"] == "acme"
    assert "company=acme" in detail.select_one(".reverse-sort")["href"]


def test_http_personal_rating_updates_counts(server):
    response = httpx.post(
        server + "/api/mark?view=exact",
        json={"key": "acme:1", "field": "group", "value": "near"},
        headers={"X-Requested-With": "jobscraper"},
    )
    assert response.status_code == 200
    assert response.json()["visible"] is False
    assert response.json()["counts"]["near"] == 2


def test_company_jobs_follow_personal_and_relevance_filters(app):
    rows, _ = app.select({"view": "companies", "company": "acme"})
    assert {r.posting.key for r in rows} == {"acme:1", "acme:2", "acme:3"}
    app.mark("acme:1", "hidden", True)
    app.mark("acme:4", "group", "near")
    rows, _ = app.select({"view": "companies", "company": "acme"})
    assert {r.posting.key for r in rows} == {"acme:2", "acme:3", "acme:4"}


def test_rejected_and_hidden_jobs_do_not_display_or_sort_by_salary(app):
    from jobscraper.classification import salaries

    st = app.state()
    for key in ("acme:1", "acme:4"):
        st.postings[key].salary = salaries.serialize(
            salaries.Salary(
                minimum=50000, maximum=70000, period="year", currency="USD", confidence="low"
            ),
            "x",
        )
    state.save(st, app.state_dir)
    app.mark("acme:4", "starred", True)
    app.mark("acme:1", "hidden", True)
    assert "AI-estimated pay" not in app.index({"view": "starred"})
    assert "AI-estimated pay" not in app.index({"view": "hidden"})
    rejected = next(r for r in app.rows() if r.posting.key == "acme:4")
    assert app.sort_key(rejected, "salary")[0] is True


def test_move_mode_replaces_rating_select_and_applied_label(app):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(app.index({"view": "relevant"}), "html.parser")
    assert soup.select_one("#move-mode").text == "Move"
    assert not soup.select(".reclassify")
    assert len(soup.select(".card-check")) == len(soup.select(".card"))
    assert all(button.text == "Applied" for button in soup.select('[data-field="applied"]'))
    app.mark("acme:1", "applied", True)
    assert "✓ Applied" in app.index({"view": "applied"})


def test_bulk_move_rating_updates_all_selected_and_preserves_notes(app):
    app.mark("acme:1", "note", "Keep this note")
    app.mark("acme:1", "hidden", True)
    app.mark("acme:2", "applied", True)
    assert app.move(["acme:1", "acme:2"], "maybe") == 2
    assert keys(app, "maybe") == ["acme:1", "acme:2", "acme:3"]
    marks = jsonfile.read(app.marks_path)
    assert marks["acme:1"]["note"] == "Keep this note"
    assert marks["acme:1"]["hidden"] is False
    assert marks["acme:2"]["applied"] is False
    assert "applied_at" not in marks["acme:2"]


@pytest.mark.parametrize("target", ["starred", "applied", "hidden"])
def test_bulk_move_turns_on_normal_mark(app, target):
    app.mark("acme:1", "hidden", True)
    app.move(["acme:1", "acme:2"], target)
    marks = jsonfile.read(app.marks_path)
    assert all(marks[k][target] for k in ["acme:1", "acme:2"])
    assert all(k in keys(app, target) for k in ["acme:1", "acme:2"])
    if target == "applied":
        assert marks["acme:1"]["applied_at"]


def test_bulk_move_bad_selection_is_atomic(app):
    app.mark("acme:1", "note", "untouched")
    before = app.marks_path.read_bytes()
    with pytest.raises(KeyError):
        app.move(["acme:1", "missing:key"], "hidden")
    assert app.marks_path.read_bytes() == before
    with pytest.raises(ValueError):
        app.move(["acme:1", "acme:6"], "exact")
    assert app.marks_path.read_bytes() == before
    for invalid in [[], "acme:1", [False]]:
        with pytest.raises(ValueError):
            app.move(invalid, "starred")
    with pytest.raises(ValueError):
        app.move(["acme:1"], "companies")


def test_bulk_move_cannot_bypass_geography(app):
    from dataclasses import replace

    app._base_cfg = replace(
        app.cfg, companies=[replace(c, locations=("SC",)) for c in app.cfg.companies]
    )
    app._use_config(app._base_cfg)
    st = app.state()
    st.postings["acme:1"].location = "Toronto, ON"
    state.save(st, app.state_dir)
    with pytest.raises(ValueError):
        app.move(["acme:1"], "exact")
    assert jsonfile.read(app.marks_path) == {}


def test_http_bulk_move_requires_header_and_moves_selection(server, app):
    payload = {"keys": ["acme:1", "acme:2"], "destination": "starred"}
    assert httpx.post(server + "/api/move", json=payload).status_code == 403
    response = httpx.post(
        server + "/api/move", json=payload, headers={"X-Requested-With": "jobscraper"}
    )
    assert response.status_code == 200
    assert response.json()["moved"] == 2
    assert keys(app, "starred") == ["acme:1", "acme:2"]


def test_industrial_is_separate_from_matches_and_can_be_manually_accepted(app):
    from jobscraper.classification import salaries

    st = app.state()
    p = posting("acme:controls", "PLC Controls Engineer", relevance="industrial")
    p.salary = {
        "estimate": {
            "minimum": 60000,
            "maximum": 80000,
            "period": "year",
            "currency": "USD",
            "confidence": "medium",
        }
    }
    st.postings[p.key] = p
    state.save(st, app.state_dir)
    assert keys(app, "industrial") == [p.key]
    assert p.key not in keys(app, "relevant")
    assert p.key not in keys(app, "exact")
    assert not salaries.eligible(p, PROFILE)
    page = app.index({"view": "industrial"})
    assert 'data-view="industrial"' in page
    assert "PLC Controls Engineer" in page
    assert "AI-estimated pay" not in page
    assert "Use Move" in page
    app.move([p.key], "near")
    assert p.key not in keys(app, "industrial")
    assert p.key in keys(app, "near")
    assert salaries.eligible(p, PROFILE, marks={"group": "near"})
    app.move([p.key], "industrial")
    assert p.key in keys(app, "industrial")
    assert not salaries.eligible(p, PROFILE, marks={"group": "industrial"})


def test_industrial_review_does_not_bypass_geography_or_personal_marks(app):
    from jobscraper.classification import classify

    st = app.state()
    p = posting("acme:controls", "Robot Programmer", relevance="industrial")
    st.postings[p.key] = p
    state.save(st, app.state_dir)
    app.mark(p.key, "hidden", True)
    assert p.key not in keys(app, "industrial")
    assert p.key in keys(app, "hidden")
    assert (
        classify.posting_group(p, PROFILE, type("Scope", (), {"area": lambda self, job: None})())
        is None
    )


def jev_record(kind, level, version="v1"):
    from test_jev_triage import triage_response

    return {"response": triage_response(kind, level, version)}


@pytest.fixture
def jev_app(app):
    jsonfile.write(
        jev_triage.store_path(app.jev_dir, "v1"),
        {
            "acme:1": jev_record("developer", "mid"),
            "acme:2": jev_record("developer", "entry"),
            "acme:4": jev_record("helpdesk", "unsure"),
            "acme:5": jev_record("computing", "unsure"),
        },
    )
    return app


def jev_keys(app, view, version="v1"):
    return sorted(r.posting.key for r in app.rows("jev", version) if app.in_view(r, view))


def test_jev_page_groups_by_jev_only(jev_app):
    assert jev_keys(jev_app, "exact") == ["acme:2"]
    assert jev_keys(jev_app, "near") == ["acme:5"]  # Sonnet called it senior; Jev doesn't
    assert jev_keys(jev_app, "maybe") == ["acme:1", "acme:4"]
    assert keys(jev_app, "exact") == ["acme:1", "mus:9"]  # main page unchanged


def test_jev_page_ignores_main_page_moves_but_keeps_marks(jev_app):
    jev_app.move(["acme:3"], "exact")
    jev_app.mark("acme:2", "hidden", True)
    assert jev_keys(jev_app, "exact") == []
    assert jev_keys(jev_app, "hidden") == ["acme:2"]
    assert "acme:3" in keys(jev_app, "exact")


def test_jev_page_renders_answers_and_links(jev_app):
    html = jev_app.index({"view": "maybe"}, "jev")
    assert "Web Developer" in html and "Forklift Operator" in html
    assert "<strong>Jev:</strong> Developer 90% · Mid 90%" in html
    assert "<strong>Sonnet:</strong> Exact" in html
    assert 'href="/jev?view=exact' in html and 'href="/?view' not in html
    assert "jev=v1" in html  # v2 has no answers yet, so links stay on v1
    assert 'data-view="companies" ' not in html
    assert 'id="move-mode"' not in html
    assert "Jev answered 4 of" in html


def test_http_jev_page_and_mark_counts(server, jev_app):
    assert httpx.get(server + "/jev").status_code == 200
    body = json.dumps({"key": "acme:2", "field": "starred", "value": True, "page": "/jev"})
    headers = {"Content-Type": "application/json", "X-Requested-With": "jobscraper"}
    resp = httpx.post(server + "/api/mark?view=exact", content=body, headers=headers)
    assert resp.json()["counts"]["exact"] == 0
    assert "companies" not in resp.json()["counts"]


def test_jev_versions_are_separate_and_selectable(jev_app):
    jsonfile.write(
        jev_triage.store_path(jev_app.jev_dir, "v2"),
        {
            "acme:3": jev_record("industrial_programming", "mid", "v2"),
            "acme:4": jev_record("code_adjacent", "entry", "v2"),
        },
    )
    assert jev_app.jev_version({}) == "v2"
    assert jev_keys(jev_app, "industrial", "v2") == ["acme:3"]
    assert jev_keys(jev_app, "near", "v2") == ["acme:4"]
    assert jev_keys(jev_app, "exact", "v1") == ["acme:2"]  # v1 answers untouched

    html = jev_app.index({"view": "near"}, "jev")
    assert "Code-adjacent 90% · Entry 90%" in html
    assert 'name="jev" value="v2"' in html
    assert "Jev questions: <a href=" in html and "<strong>v2</strong>" in html
    v1 = jev_app.index({"view": "exact", "jev": "v1"}, "jev")
    assert "Web Developer" not in v1 and "Data Analyst" in v1


def test_jev_v3_drops_industrial_tab_and_flags_low_confidence(jev_app):
    record = jev_record("industrial_automation", "entry", "v3")
    record["response"]["answers"]["kind"]["confidence"] = 0.4
    jsonfile.write(jev_triage.store_path(jev_app.jev_dir, "v3"), {"acme:3": record})
    assert jev_app.jev_version({}) == "v3"  # newest version with answers
    assert jev_keys(jev_app, "maybe", "v3") == ["acme:3"]
    html = jev_app.index({"view": "maybe"}, "jev")
    assert 'data-view="industrial" ' not in html
    assert "Industrial / automation 40% · Entry 90% · Low confidence" in html
    v2_html = jev_app.index({"view": "exact", "jev": "v1"}, "jev")
    assert "<strong>v3</strong>" not in v2_html


def test_jev_v5_evaluate_tab_full_text_and_stale_answer_handling(app):
    from test_jev_v5 import description, v5_response

    st = state.load(app.state_dir)
    exact, evaluate = st.postings["acme:1"], st.postings["acme:2"]
    cached = {exact.key: description(exact, cfg=app.cfg)}
    jsonfile.write(app.state_dir / "descriptions.json", cached)
    records = {}
    for p, response in (
        (exact, v5_response()),
        (evaluate, v5_response("code_adjacent", degree="unclear", fit="evaluate")),
    ):
        body = jev_triage.request_body(p, app.cfg, "v5", description=cached.get(p.key))
        records[p.key] = {
            "signature": jev_triage.signature(body),
            "request": body,
            "response": response,
        }
    jsonfile.write(jev_triage.store_path(app.jev_dir, "v5"), records)
    assert app.jev_version({}) == "v5"
    assert jev_keys(app, "exact", "v5") == [exact.key]
    assert jev_keys(app, "evaluate", "v5") == [evaluate.key]
    html = app.index({"view": "evaluate", "jev": "v5"}, "jev")
    assert 'data-view="evaluate"' in html
    assert 'data-view="near" ' not in html and 'data-view="maybe" ' not in html
    assert "Degree unclear" in html and "Full description unavailable" in html
    assert "Evaluate 90%" in html
    assert "Evaluate</th>" in html
    assert 'data-view="evaluate" ' not in app.index({}, "sonnet")
    cached[exact.key]["text"] += " New qualifications."
    jsonfile.write(app.state_dir / "descriptions.json", cached)
    assert jev_keys(app, "exact", "v5") == []
