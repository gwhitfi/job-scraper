import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from jobscraper import activity, http, jsonfile


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
