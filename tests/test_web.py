import json
import os
import threading
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from jobscraper import state, web
from jobscraper.config import Config, Notifications, Profile
from jobscraper.models import Company
from jobscraper.state import Health, Posting, State

NOW = datetime.now(UTC)
PROFILE = Profile(target_role="dev", levels=("entry", "mid"))


def posting(key, title, relevance="core", level="mid", status="active", employer="", **kw):
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
        classification={"relevance": relevance, "level": level, "reason": f"{title} reason"},
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
            ]
        },
        health={
            "acme": Health(last_success=NOW),
            "mus": Health(consecutive_failures=3, last_error="403"),
        },
    )
    state.save(st, tmp_path / "state")
    return web.App(cfg, tmp_path / "state", tmp_path / "marks.json")


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
