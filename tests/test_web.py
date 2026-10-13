import json
import os
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from jobscraper import state, web
from jobscraper.config import Config, Notifications, Profile
from jobscraper.models import Company
from jobscraper.state import Health, Posting, State

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


def test_mark_unknown_posting(app):
    with pytest.raises(KeyError):
        app.mark("acme:999", "starred", True)


def test_page_renders(app):
    html = app.index({"view": "exact"})
    assert "Web Developer" in html
    assert "AFL (via Move Upstate SC)" in html
    assert "Data Analyst" not in html
    assert "Site checks failing: Move Upstate SC" in html
    assert 'class="badge new"' in html  # acme:1 first seen today
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
    ("now", "expected"),
    [
        (datetime(2026, 10, 13, 0, 0, tzinfo=UTC), datetime(2026, 10, 13, 0, 17, tzinfo=UTC)),
        (datetime(2026, 10, 13, 0, 17, tzinfo=UTC), datetime(2026, 10, 13, 2, 17, tzinfo=UTC)),
        (datetime(2026, 10, 13, 1, 30, tzinfo=UTC), datetime(2026, 10, 13, 2, 17, tzinfo=UTC)),
        (datetime(2026, 10, 13, 23, 40, tzinfo=UTC), datetime(2026, 10, 14, 0, 17, tzinfo=UTC)),
    ],
)
def test_next_run(now, expected):
    assert web.next_run(now) == expected


def test_schedule_matches_workflow():
    workflow = (Path(__file__).parent.parent / ".github/workflows/check.yml").read_text()
    cron = f'cron: "{web.SCHEDULE_MINUTE} */{web.SCHEDULE_EVERY_HOURS} * * *"'
    assert cron in workflow


def test_header_shows_next_update(app):
    assert "· next ~" in app.index({})


def test_stale_banner(app):
    st = app.state()
    assert "No update in" not in app.index({})
    for h in st.health.values():
        if h.last_success:
            h.last_success = NOW - timedelta(hours=web.STALE_HOURS + 1)
    assert f"No update in {web.STALE_HOURS + 1} hours" in app.index({})


def test_email_failure_banner(app, tmp_path):
    from jobscraper import notify

    outbox = notify.load_outbox(tmp_path / "state" / "email.json")
    notify.queue(outbox, {"subject": "s", "text": "t", "html": "h"})
    outbox["last_error"] = "SMTPAuthenticationError: 535 bad <creds>"
    notify.save_outbox(tmp_path / "state" / "email.json", outbox)
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
    from jobscraper import usage

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
