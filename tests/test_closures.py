from datetime import UTC, date, datetime, timedelta

import httpx

from jobscraper.models import Company, RawPosting
from jobscraper.scraping import crawl
from jobscraper.storage.state import State
from jobscraper.workflow import closures, tracker

NOW = datetime(2026, 10, 20, tzinfo=UTC)


def raw(source_id):
    return RawPosting(source_id, "Developer", "SC", f"https://acme.example/job/{source_id}")


def test_confirm_closes_only_verified_unprotected_jobs_and_preserves_history():
    state = State()
    tracker.apply_results(state, "acme", [raw(str(i)) for i in range(1, 7)], NOW)
    state.postings["acme:4"].status = "removed"
    earlier = state.postings["acme:4"].removed_at = NOW - timedelta(days=1)
    marks = {"acme:2": {"starred": True}, "acme:3": {"applied": True}, "acme:1": {"hidden": True}}
    evidence = {f"acme:{i}": {"status": "closed"} for i in range(1, 6)}
    evidence["acme:5"]["status"] = "unknown"
    evidence["unknown:1"] = {"status": "closed"}
    assert closures.confirm(state, evidence, marks, NOW) == ["acme:1"]
    assert len(state.postings) == 6
    assert state.postings["acme:1"].status == "removed"
    assert state.postings["acme:1"].removed_at == NOW
    assert state.postings["acme:2"].status == "active"
    assert state.postings["acme:3"].status == "active"
    assert state.postings["acme:4"].removed_at == earlier
    assert state.postings["acme:5"].status == "active"
    assert state.postings["acme:6"].status == "active"
    assert marks["acme:1"] == {"hidden": True}
    assert closures.confirm(state, evidence, marks, NOW) == []


def test_stale_closed_results_and_request_errors_cannot_reopen_but_readable_jobs_can(monkeypatch):
    monkeypatch.setattr(crawl, "public_host", lambda url: None)
    company = Company("acme", "Acme", "generic", "https://acme.example/jobs", date(2026, 1, 1))
    requests = []
    active = '<script type="application/ld+json">'
    active += '{"@type":"JobPosting","title":"Developer","description":"Build software"}</script>'

    def handler(request):
        requests.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nCrawl-delay: 0\n")
        if request.url.path == "/job/1":
            return httpx.Response(
                200, text=active + '<div class="job-manager-info">This listing has expired.</div>'
            )
        if request.url.path == "/job/2":
            return httpx.Response(503)
        assert request.url.path == "/job/3"
        return httpx.Response(200, text=active)

    evidence = {f"acme:{i}": {"status": "closed"} for i in (1, 2, 3)}
    evidence["acme:4"] = {"status": "unknown"}
    evidence["acme:5"] = {"status": "closed"}
    state = State()
    tracker.apply_results(state, "acme", [raw(str(i)) for i in range(1, 6)], NOW)
    for i in (1, 2, 3):
        state.postings[f"acme:{i}"].status = "removed"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = closures.verify_reappearances(
            [raw(str(i)) for i in range(1, 6)], company, client, state, evidence, NOW
        )
    assert [p.source_id for p in result] == ["3", "4", "5"]
    assert requests == ["/robots.txt", "/job/1", "/job/2", "/job/3"]
