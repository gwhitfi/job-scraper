import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from jobscraper import activity
from jobscraper.scraping import http
from jobscraper.storage import jsonfile


def test_trace_persists_progress_failure_and_redacts(tmp_path):
    path = tmp_path / "discovery-history.json"
    with (
        pytest.raises(ValueError),
        activity.Trace(path, "add_company", url="user:pass@example.com/jobs?token=secret"),
    ):
        activity.emit("validation_started")
        current = jsonfile.read(tmp_path / "discovery-current.json")
        assert current["status"] == "running"
        assert current["events"][-1]["stage"] == "validation_started"
        raise ValueError("Failed https://example.com/jobs?api_key=private")
    saved = path.read_text()
    assert "secret" not in saved and "private" not in saved and "user:pass" not in saved
    record = json.loads(saved)["runs"][0]
    assert record["status"] == "failed" and "finished" in record
    activity.emit("outside")
    assert path.read_text() == saved


def test_retention_and_interrupted_trace(tmp_path):
    path = tmp_path / "crawl-history.json"
    for _ in range(51):
        with activity.Trace(path, "crawl"):
            pass
    assert len(jsonfile.read(path)["runs"]) == 50
    current_path = tmp_path / "crawl-current.json"
    previous = jsonfile.read(current_path)
    previous.update(id="interrupted", status="running")
    jsonfile.write(current_path, previous)
    with activity.Trace(path, "crawl"):
        pass
    records = jsonfile.read(path)["runs"]
    assert len(records) == 50
    assert records[1]["id"] == "interrupted"
    assert records[1]["status"] == "running"


def test_parallel_traces_are_isolated(tmp_path):
    def work(i):
        with activity.Trace(tmp_path / f"{i}-history.json", "crawl"):
            activity.emit("company_started", company=str(i))
            activity.emit("response", status_code=200)

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(work, range(2)))
    for i in range(2):
        record = jsonfile.read(tmp_path / f"{i}-history.json")["runs"][0]
        assert record["events"][2]["company"] == str(i)


def test_http_trace_redirect_and_status_without_body_or_headers(tmp_path):
    path = tmp_path / "crawl-history.json"

    def handle(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/jobs"})
        return httpx.Response(403, text="private body", headers={"Set-Cookie": "private cookie"})

    with http.client() as configured:
        hooks = configured.event_hooks
    with (
        activity.Trace(path, "crawl"),
        httpx.Client(
            transport=httpx.MockTransport(handle), event_hooks=hooks, follow_redirects=True
        ) as client,
    ):
        response = client.get(
            "https://example.com/start", headers={"Authorization": "private auth"}
        )
        assert response.status_code == 403
    saved = path.read_text()
    assert "private" not in saved
    events = json.loads(saved)["runs"][0]["events"]
    assert [e["status_code"] for e in events if e["stage"] == "response"] == [302, 403]
    assert [e["url"] for e in events if e["stage"] == "request"] == [
        "https://example.com/start",
        "https://example.com/jobs",
    ]


def test_page_escapes_content_and_includes_current_once(tmp_path):
    path = tmp_path / "discovery-history.json"
    with activity.Trace(path, "add_company") as trace:
        activity.emit("outcome", message="<script>bad()</script>")
    html = activity.page([path], trace.record["id"])
    assert "<script>bad()" not in html
    assert "&lt;script&gt;" in html
    assert html.count(f'id="{trace.record["id"]}"') == 1
    assert "<summary>" in html and " open>" in html


def test_logging_failure_does_not_break_work(tmp_path, monkeypatch):
    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(jsonfile, "write", fail)
    with activity.Trace(tmp_path / "crawl-history.json", "crawl"):
        activity.emit("work_completed")


def test_page_distinguishes_geography_and_classification_progress(tmp_path):
    path = tmp_path / "crawl-history.json"
    with activity.Trace(path, "crawl") as trace:
        activity.emit("location_filter", company="hospital", unknown_kept=0)
        activity.emit("company_completed", company="hospital", kept=671)
        activity.emit("classification_progress", completed=50, total=671, calls=1)
    html = activity.page([path], trace.record["id"])
    assert "50 / 671 jobs complete" in html
    assert "In location scope" in html
    assert "before relevance classification" in html
    assert "Unknown location kept" in html


def test_log_summary_prioritizes_failures_and_uses_company_names(tmp_path):
    from bs4 import BeautifulSoup

    path = tmp_path / "crawl-history.json"
    with activity.Trace(path, "crawl") as trace:
        activity.emit("company_started", company="good-123", name="Good Company")
        activity.emit("company_completed", company="good-123", kept=10)
        activity.emit("company_started", company="bad-456", name="Failed Company")
        activity.emit("company_failed", company="bad-456", error="HTTP 403 <denied>")
        trace.record["status"] = "partial_failure"
    soup = BeautifulSoup(activity.page([path]), "html.parser")
    assert "Completed with issues" in soup.select_one(".run > summary").text
    assert "Failed Company" in soup.select_one(".issues").text
    assert "HTTP 403 <denied>" in soup.select_one(".issues").text
    assert "Failed Company" in soup.select_one("tbody tr").text
    assert soup.select_one(".event:last-child")["data-level"] == "warning"


def test_event_filters_preserve_errors_and_expandable_details(tmp_path):
    from bs4 import BeautifulSoup

    path = tmp_path / "crawl-history.json"
    with activity.Trace(path, "crawl"):
        activity.emit("request", url="https://example.com/jobs")
        activity.emit("response", status_code=403, url="https://example.com/jobs")
        activity.emit("retry", delay_seconds=10)
    soup = BeautifulSoup(activity.page([path]), "html.parser")
    errors = soup.select('.event[data-level="error"]')
    assert len(errors) == 1
    assert "HTTP 403" in errors[0].summary.text
    assert errors[0].select_one("details pre")
    assert soup.select_one('.event[data-level="warning"]')
    assert [o["value"] for o in soup.select("#event-level option")] == [
        "milestones",
        "issues",
        "all",
    ]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-10-15T14:00:00+00:00", "Oct 15, 2026 · 10:00 AM EDT"),
        ("2026-12-15T15:00:00+00:00", "Dec 15, 2026 · 10:00 AM EST"),
        ("2026-10-15T02:00:00+00:00", "Oct 14, 2026 · 10:00 PM EDT"),
    ],
)
def test_log_times_are_eastern(value, expected):
    from jobscraper.presentation.activity_view import stamp

    assert stamp(value) == expected
