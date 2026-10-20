"""Bounded, persistent crawl and discovery traces; no response bodies or headers."""

import logging
import re
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from jobscraper.presentation import activity_view
from jobscraper.storage import jsonfile

_current: ContextVar["Trace | None"] = ContextVar("activity", default=None)
SECRET = re.compile(r"token|key|secret|password|signature|auth|cookie", re.I)


def safe_url(value: str) -> str:
    try:
        p = urlsplit(value if "://" in value else "https://" + value)
        query = urlencode(
            [
                (k, "[redacted]" if SECRET.search(k) else v)
                for k, v in parse_qsl(p.query, keep_blank_values=True)
            ]
        )
        return urlunsplit((p.scheme, p.netloc.split("@")[-1], p.path, query, ""))
    except ValueError:
        return "[invalid URL]"


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: "[redacted]"
            if SECRET.search(k)
            else safe_url(v)
            if k == "url" and isinstance(v, str)
            else clean(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"https?://[^\s'\"<>]+", lambda m: safe_url(m[0]), value)[:2000]
    return value


def emit(stage: str, **details) -> None:
    trace = _current.get()
    if trace:
        if stage == "company_started":
            trace.company = details["company"]
        if trace.company:
            details.setdefault("company", trace.company)
        trace.event(stage, **details)
        if stage in ("company_completed", "company_failed"):
            trace.company = None


class Trace:
    def __init__(self, path: Path | None, kind: str, **details):
        self.path = path
        self.current_path = (
            path.with_name(path.name.replace("-history", "-current")) if path else None
        )
        self.started = time.monotonic()
        self.company: str | None = None
        self.record = dict(
            id=uuid.uuid4().hex,
            kind=kind,
            started=datetime.now(UTC).isoformat(),
            status="running",
            details=clean(details),
            events=[],
        )

    def __enter__(self) -> "Trace":
        if self.current_path:
            try:
                previous = jsonfile.read(self.current_path)
                if previous:
                    self.archive(previous)
            except (OSError, ValueError):
                logging.exception("Could not preserve previous activity log")
        self.token = _current.set(self)
        self.event("started", **self.record["details"])
        return self

    def event(self, stage: str, **details) -> None:
        events = self.record["events"]
        if len(events) >= 3000:
            del events[1]
            self.record["omitted_events"] = self.record.get("omitted_events", 0) + 1
        events.append(dict(time=datetime.now(UTC).isoformat(), stage=stage, **clean(details)))
        self.save()

    def archive(self, record: dict) -> None:
        if self.path is None:
            return
        records = jsonfile.read(self.path).get("runs", [])
        records = [r for r in records if r["id"] != record["id"]]
        jsonfile.write(self.path, {"runs": [record, *records][:50]})

    def save(self) -> None:
        if self.current_path is None:
            return
        try:
            jsonfile.write(self.current_path, self.record)
        except (OSError, ValueError):
            logging.exception("Could not save activity log %s", self.current_path)

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc:
                self.record["status"] = "failed"
                self.event("error", error=f"{type(exc).__name__}: {exc}")
            elif self.record["status"] == "running":
                self.record["status"] = "completed"
            self.record.update(
                finished=datetime.now(UTC).isoformat(),
                seconds=round(time.monotonic() - self.started, 2),
            )
            self.event("finished", status=self.record["status"])
            try:
                self.archive(self.record)
            except (OSError, ValueError):
                logging.exception("Could not archive activity log")
        finally:
            _current.reset(self.token)


def request_hook(request: httpx.Request) -> None:
    request.extensions["trace_started"] = time.monotonic()
    emit("request", method=request.method, url=safe_url(str(request.url)))


def response_hook(response: httpx.Response) -> None:
    started = response.request.extensions.get("trace_started", time.monotonic())
    emit(
        "response",
        method=response.request.method,
        url=safe_url(str(response.url)),
        status_code=response.status_code,
        seconds=round(time.monotonic() - started, 3),
    )


def page(paths: list[Path], selected: str = "") -> str:
    records = []
    for path in paths:
        try:
            saved = jsonfile.read(path).get("runs", [])
            current = jsonfile.read(path.with_name(path.name.replace("-history", "-current")))
            if current:
                saved = [current, *[r for r in saved if r["id"] != current["id"]]][:50]
            records.extend(saved)
        except (OSError, ValueError):
            continue
    records.sort(key=lambda r: r["started"], reverse=True)
    selected = selected or (records[0]["id"] if records else "")
    parts = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        "<title>Crawl logs</title><style>",
        activity_view.STYLE,
        '</style></head><body><main><div class="page-top"><a href="/">← Jobs</a>',
        '<button class="button" onclick="location.reload()">Refresh logs</button></div>',
        '<h1>Crawl logs</h1><p class="hint">Crawl outcomes, source health, and progress. '
        "Latest 50 runs of each type: crawls, description collection, and add-company attempts. "
        "Times shown in US Eastern (EST/EDT).</p>",
    ]
    if not records:
        parts.append("<p>No logs yet. Logs appear after the next crawl or add-company attempt.</p>")
    parts.extend(activity_view.render_record(r, selected) for r in records)
    return "".join(parts) + "</main><script>" + activity_view.SCRIPT + "</script></body></html>"
