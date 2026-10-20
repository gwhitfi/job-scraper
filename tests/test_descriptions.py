import argparse
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import fixture_text, mock_client

from jobscraper import activity, cli, config
from jobscraper.scraping import crawl, descriptions
from jobscraper.storage import jsonfile, state
from jobscraper.storage.state import Posting, State

NOW = datetime(2026, 10, 19, tzinfo=UTC)
TITLE = "Software Developer I"


def posting(**kw):
    values = dict(
        key="acme:R1",
        company_id="acme",
        source_id="R1",
        title=TITLE,
        location="Greenville, SC",
        url="https://acme.example/job/R1",
        posted_date=None,
        first_seen=NOW,
        last_seen=NOW,
        summary="Short teaser",
    )
    return Posting(**(values | kw))


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("""
[profile]
target_role = "software developer"
[notifications]
email = "me@example.com"
[[companies]]
id = "acme"
name = "Acme"
adapter = "generic"
url = "https://acme.example/jobs"
added = 2026-01-01
""")
    return config.load(path)


def page(title=TITLE, description="<p>Build software.</p>", **fields):
    data = {"@type": "JobPosting", "title": title, "description": description} | fields
    return '<script type="application/ld+json">' + json.dumps(data) + "</script>"


def collect(tmp_path, cfg, handler, postings=None, **kw):
    return descriptions.collect(
        postings if postings is not None else [posting()],
        cfg,
        tmp_path / "descriptions.json",
        mock_client(handler),
        now=NOW,
        validate=lambda u: None,
        sleep=lambda d: None,
        **kw,
    )


def test_full_text_preserves_late_qualifications_and_separate_requirements():
    text = "<p>Responsibilities: " + "Build useful software. " * 200 + "</p>"
    text += "<p>Required: 0–2 years. Preferred: Computer Science degree.</p>"
    result, method = descriptions.parse_html(
        page(description=text, educationRequirements="Bachelor of Computer Science"), posting()
    )
    assert len(result) > 2000
    assert "Required: 0–2 years" in result
    assert "Preferred: Computer Science degree" in result
    assert "educationRequirements: Bachelor of Computer Science" in result
    assert method == "schema.org/JobPosting"


def test_nested_jsonld_selects_requested_job_and_decodes_encoded_html():
    items = [
        {"@type": "JobPosting", "title": "Sales", "description": "Wrong job"},
        {
            "@type": ["JobPosting"],
            "title": TITLE,
            "description": "&lt;p&gt;Computer Science &amp;amp; mathematics&lt;/p&gt;",
        },
    ]
    source = '<script type="application/ld+json">' + json.dumps({"@graph": items}) + "</script>"
    text, _ = descriptions.parse_html(source, posting())
    assert text == "Computer Science & mathematics"


def test_jsonld_prefers_job_identity_over_a_shared_title():
    records = [
        {"@type": "JobPosting", "title": TITLE, "identifier": "R2", "description": "Wrong job"},
        {
            "@type": "JobPosting",
            "title": TITLE,
            "identifier": {"value": "R1"},
            "description": "Correct job",
        },
    ]
    source = '<script type="application/ld+json">' + json.dumps(records) + "</script>"
    assert descriptions.parse_html(source, posting())[0] == "Correct job"
    with pytest.raises(descriptions.Unavailable, match="Multiple jobs"):
        descriptions.parse_html(source, posting(source_id="unknown"))


@pytest.mark.parametrize(
    "source",
    [
        "<h1>Careers</h1><main>Search jobs and join our team.</main>",
        page(title="Different job"),
        '<h1>Different job</h1><div class="jobdescription">Not this job</div>',
        "<h1>Software Developer I</h1><main>Navigation and related jobs</main>",
    ],
)
def test_generic_shell_or_wrong_job_is_not_a_full_description(source):
    with pytest.raises(descriptions.Unavailable):
        descriptions.parse_html(source, posting())


def test_matching_html_detail_excludes_navigation_and_form():
    source = f'<h1>{TITLE}</h1><nav>Careers</nav><div class="jobdescription">'
    source += "<p>Requires Computer Science.</p><form>Send resume</form></div>"
    assert descriptions.parse_html(source, posting()) == (
        "Requires Computer Science.",
        ".jobdescription",
    )


def test_html_requirements_outside_description_are_preserved():
    source = f'<h1>Job title: {TITLE}</h1><div class="jobdescription">Write code.</div>'
    source += '<div itemprop="educationRequirements">Computer Science preferred.</div>'
    text, _ = descriptions.parse_html(source, posting())
    assert "Write code." in text and "Computer Science preferred." in text


def test_workday_uses_full_detail_api(cfg, tmp_path):
    company = replace(
        cfg.companies[0], adapter="workday", url="https://acme.wd1.myworkdayjobs.com/External"
    )
    cfg = replace(cfg, companies=[company])
    p = posting(url="https://acme.wd1.myworkdayjobs.com/External/job/Greenville/Developer_R1")

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        assert request.url.path == "/wday/cxs/acme/External/job/Greenville/Developer_R1"
        return httpx.Response(
            200,
            json={
                "jobPostingInfo": {
                    "title": TITLE,
                    "jobDescription": "<p>Required: Computer Science; 0–2 years.</p>",
                }
            },
        )

    assert collect(tmp_path, cfg, handler, [p])["ready"] == 1
    saved = jsonfile.read(tmp_path / "descriptions.json")[p.key]
    assert saved["text"] == "Required: Computer Science; 0–2 years."
    assert saved["fetched_at"] == NOW.isoformat()
    assert saved["method"].startswith("workday/")


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ({}, "ready"),
        (
            {"jobOpeningShareUrl": "https://thapremierclientkp.bamboohr.com/careers/99"},
            "unavailable",
        ),
        ({"description": "<p><br></p>", "minimumExperience": "Entry"}, "unavailable"),
        ({"jobOpeningStatus": "Closed"}, "unavailable"),
    ],
)
def test_bamboohr_public_detail_fixture(cfg, tmp_path, mutation, expected):
    data = json.loads(fixture_text("description_bamboohr.json"))
    data["result"]["jobOpening"].update(mutation)
    company = replace(
        cfg.companies[0], adapter="bamboohr", url="https://thapremierclientkp.bamboohr.com/careers"
    )
    cfg = replace(cfg, companies=[company])
    p = posting(source_id="9", title="FUTURE OPEN POSITIONS", url=company.url + "/9")

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        assert request.url.path == "/careers/9/detail"
        return httpx.Response(200, json=data)

    assert collect(tmp_path, cfg, handler, [p])[expected] == 1
    saved = jsonfile.read(tmp_path / "descriptions.json")[p.key]
    if expected == "ready":
        assert len(saved["text"]) > 1000
        assert saved["method"] == "bamboohr/jobOpening.description"


@pytest.mark.parametrize("removed", [True, False])
def test_healthcaresource_resolves_posting_id_and_handles_removed_jobs(cfg, tmp_path, removed):
    company = replace(
        cfg.companies[0], adapter="healthcaresource", url="https://pm.healthcaresource.com/cs/anmed"
    )
    cfg = replace(cfg, companies=[company])
    p = posting(source_id="25577", title="Coder I - Professional")
    data = json.loads(fixture_text("description_healthcaresource.json"))
    calls = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls.append(request)
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["query"]["bool"]["filter"] == [
                {"terms": {"userArea.requisitionID": [25577]}}
            ]
            return httpx.Response(
                200, json={"hits": {"total": {"value": 1}, "hits": [{"_source": data}]}}
            )
        assert request.url.path == "/JobseekerAPI/Site/anmed/api/v2/JobPostingV2"
        assert request.url.params["id"] == "31856"
        return httpx.Response(404) if removed else httpx.Response(200, json=data)

    counts = collect(tmp_path, cfg, handler, [p])
    record = jsonfile.read(tmp_path / "descriptions.json")[p.key]
    assert len(calls) == 2
    if removed:
        assert counts["unavailable"] == 1
        assert "not found" in record["error"]
    else:
        assert counts["ready"] == 1
        assert "Duties" in record["text"]
        assert "High School Diploma or GED" in record["text"]
        assert len(record["text"]) > 2000


@pytest.mark.parametrize(
    "mismatch", ["internal", "wrong_id", "incomplete", "malformed", "wrong_detail"]
)
def test_healthcaresource_refuses_ambiguous_or_wrong_details(cfg, tmp_path, mismatch):
    company = replace(
        cfg.companies[0], adapter="healthcaresource", url="https://pm.healthcaresource.com/cs/anmed"
    )
    cfg = replace(cfg, companies=[company])
    p = posting(source_id="25577", title="Coder I - Professional")
    data = json.loads(fixture_text("description_healthcaresource.json"))
    if mismatch == "internal":
        data["userArea"]["isInternalOnly"] = True
    if mismatch == "wrong_id":
        data["userArea"]["requisitionID"] = 123

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.method == "POST":
            if mismatch == "malformed":
                return httpx.Response(200, json={"hits": None})
            return httpx.Response(
                200,
                json={
                    "hits": {
                        "total": {"value": 3 if mismatch == "incomplete" else 1},
                        "hits": [{"_source": data}],
                    }
                },
            )
        assert mismatch == "wrong_detail"
        data["userArea"]["requisitionID"] = 999
        return httpx.Response(200, json=data)

    assert collect(tmp_path, cfg, handler, [p])["unavailable"] == 1


@pytest.mark.parametrize(
    "notice",
    [
        '<div class="job-manager-info">This listing has expired.</div>',
        "<h2>This job posting is no longer active, but we’re still hiring in other areas.</h2>",
    ],
)
def test_closed_notice_prevents_accepting_stale_structured_description(notice):
    with pytest.raises(descriptions.Unavailable, match="expired or no longer active"):
        descriptions.parse_html(page() + notice, posting())


def test_oracle_qualifications_are_kept_separate_from_short_teaser(cfg, tmp_path):
    company = replace(cfg.companies[0], adapter="oracle_hcm")
    cfg = replace(cfg, companies=[company])

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        assert request.url.params["finder"] == "ById;Id=R1"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "Id": "R1",
                        "Title": TITLE,
                        "ShortDescriptionStr": "Teaser only",
                        "ExternalDescriptionStr": "<p>Build software.</p>",
                        "ExternalResponsibilitiesStr": "<p>Write and review code.</p>",
                        "ExternalQualificationsStr": "<p>Computer Science required.</p>",
                    }
                ]
            },
        )

    assert collect(tmp_path, cfg, handler)["ready"] == 1
    saved = jsonfile.read(tmp_path / "descriptions.json")["acme:R1"]
    assert "Computer Science required." in saved["text"]
    assert "Write and review code." in saved["text"]
    assert "Teaser" not in saved["text"]


def test_collection_caches_without_mutating_postings_and_detects_changes(cfg, tmp_path):
    p = posting()
    before = p.__dict__.copy()

    def handler(request):
        return (
            httpx.Response(404)
            if request.url.path == "/robots.txt"
            else httpx.Response(200, text=page())
        )

    assert collect(tmp_path, cfg, handler, iter([p]))["ready"] == 1
    assert p.__dict__ == before
    records = jsonfile.read(tmp_path / "descriptions.json")
    assert descriptions.pending([p], cfg, records, NOW + timedelta(days=6)) == []
    assert descriptions.pending([p], cfg, records, NOW + timedelta(days=7)) == [p]
    changed = replace(p, summary="New requirements")
    assert descriptions.pending([changed], cfg, records, NOW) == [changed]
    assert collect(tmp_path, cfg, lambda r: pytest.fail("Unexpected request"))["attempted"] == 0


def test_explicit_retry_selects_only_failed_or_unavailable_and_keeps_other_records(cfg, tmp_path):
    posts = [
        posting(key="acme:" + status)
        for status in ("ready", "blocked", "unavailable", "failed", "new")
    ]
    records = {
        p.key: {
            "status": p.key.split(":")[1],
            "checked_at": NOW.isoformat(),
            "signature": descriptions.signature(p, cfg.companies[0]),
        }
        for p in posts[:-1]
    }
    jsonfile.write(tmp_path / "descriptions.json", records)
    selected = descriptions.pending(posts, cfg, records, NOW, retry_unavailable=True)
    assert {p.key for p in selected} == {"acme:unavailable", "acme:failed"}

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=page())

    with activity.Trace(None, "descriptions") as trace:
        counts = collect(tmp_path, cfg, handler, posts, retry_unavailable=True)
    saved = jsonfile.read(tmp_path / "descriptions.json")
    assert counts["ready"] == 2
    assert saved["acme:blocked"] == records["acme:blocked"]
    assert saved["acme:ready"] == records["acme:ready"]
    assert "acme:new" not in saved
    events = [e for e in trace.record["events"] if e["stage"] == "description_completed"]
    assert {e["posting"] for e in events} == {"acme:unavailable", "acme:failed"}


def test_failed_refresh_keeps_last_good_text_and_is_not_ready(cfg, tmp_path):
    p = posting()
    path = tmp_path / "descriptions.json"
    old_time = (NOW - timedelta(days=8)).isoformat()
    jsonfile.write(
        path,
        {
            p.key: {
                "status": "ready",
                "text": "Previous full description",
                "fetched_at": old_time,
                "checked_at": old_time,
                "signature": descriptions.signature(p, cfg.companies[0]),
            }
        },
    )

    def handler(request):
        return httpx.Response(404) if request.url.path == "/robots.txt" else httpx.Response(503)

    assert collect(tmp_path, cfg, handler)["failed"] == 1
    saved = jsonfile.read(path)[p.key]
    assert saved["status"] == "failed"
    assert saved["text"] == "Previous full description"
    assert saved["fetched_at"] == old_time
    assert descriptions.pending([p], cfg, {p.key: saved}, NOW) == []
    assert descriptions.pending([p], cfg, {p.key: saved}, NOW + timedelta(days=1)) == [p]


def test_robots_denial_never_fetches_jobs_and_other_jobs_continue(cfg, tmp_path):
    requests = []

    def handler(request):
        requests.append(str(request.url))
        assert request.url.path == "/robots.txt"
        return httpx.Response(200, text="User-agent: *\nDisallow: /\n")

    posts = [posting(), posting(key="acme:R2", source_id="R2", url="https://acme.example/job/R2")]
    counts = collect(tmp_path, cfg, handler, posts)
    assert counts["blocked"] == 2
    assert len(requests) == 1


def test_path_specific_robots_denial_does_not_block_permitted_jobs(cfg, tmp_path):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        assert request.url.path == "/job/R1"
        return httpx.Response(200, text=page())

    posts = [posting(url="https://acme.example/private/R1"), posting(key="acme:R2")]
    counts = collect(tmp_path, cfg, handler, posts)
    assert counts["blocked"] == 1 and counts["ready"] == 1


def test_size_limit_records_unavailable_without_truncating(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(descriptions, "MAX_TEXT", 10)

    def handler(request):
        return (
            httpx.Response(404)
            if request.url.path == "/robots.txt"
            else httpx.Response(200, text=page())
        )

    assert collect(tmp_path, cfg, handler)["unavailable"] == 1
    record = jsonfile.read(tmp_path / "descriptions.json")["acme:R1"]
    assert "text" not in record
    assert "not truncated" in record["error"]


def test_pending_excludes_removed_duplicates_paused_and_outside_scope(cfg):
    cfg = replace(cfg, companies=[replace(cfg.companies[0], locations=("SC",))])
    posts = [
        posting(),
        posting(status="removed"),
        posting(duplicate_of="another"),
        posting(location="Toronto, Canada", countries=("CA",)),
    ]
    assert descriptions.pending(posts, cfg, {}, NOW) == [posts[0]]
    cfg = replace(cfg, companies=[replace(cfg.companies[0], enabled=False)])
    assert descriptions.pending(posts, cfg, {}, NOW) == []


def test_collection_attempt_budget(cfg, tmp_path):
    posts = [posting(), posting(key="acme:R2", source_id="R2")]

    def handler(request):
        return (
            httpx.Response(404)
            if request.url.path == "/robots.txt"
            else httpx.Response(200, text=page())
        )

    result = collect(tmp_path, cfg, handler, posts, limit=1)
    assert result["attempted"] == 1
    assert result["remaining"] == 1


def test_exhausted_request_budget_leaves_jobs_pending(cfg, tmp_path, monkeypatch):
    def exhausted(*args):
        raise crawl.LimitExceeded("Request budget exhausted")

    monkeypatch.setattr(descriptions, "fetch", exhausted)
    result = collect(tmp_path, cfg, lambda r: pytest.fail("No requests"))
    assert result["attempted"] == 0 and result["remaining"] == 1
    assert not (tmp_path / "descriptions.json").exists()


def test_collection_stops_at_time_budget_with_progress_saved(cfg, tmp_path, monkeypatch):
    clock = [0]
    monkeypatch.setattr(descriptions.time, "monotonic", lambda: clock[0])
    posts = [posting(), posting(key="acme:R2", source_id="R2")]

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=page())

    def progress(p, r):
        clock[0] = 121

    result = collect(tmp_path, cfg, handler, posts, max_seconds=120, progress=progress)
    assert result["attempted"] == 1 and result["remaining"] == 1
    assert len(jsonfile.read(tmp_path / "descriptions.json")) == 1


def test_cli_collection_has_no_ai_email_or_posting_writes(cfg, tmp_path, monkeypatch):
    state.save(State(postings={"acme:R1": posting()}), tmp_path / "state")
    before = (tmp_path / "state/postings.json").read_bytes()
    monkeypatch.setattr(cli, "_llm_client", lambda *a: pytest.fail("AI must not be called"))
    monkeypatch.setattr(cli.jev, "evaluate", lambda *a: pytest.fail("Jev must not be called"))
    monkeypatch.setattr(cli, "_send_alerts", lambda *a: pytest.fail("Email must not be sent"))

    def handler(request):
        return (
            httpx.Response(404)
            if request.url.path == "/robots.txt"
            else httpx.Response(200, text=page())
        )

    monkeypatch.setattr(cli.http, "client", lambda: mock_client(handler))
    monkeypatch.setattr(descriptions.crawl, "public_host", lambda u: None)
    cli.cmd_collect_descriptions(
        argparse.Namespace(
            config=tmp_path / "config.toml",
            data_dir=tmp_path,
            state_dir=tmp_path / "state",
            limit=1,
            max_seconds=10,
            company=None,
            dry_run=False,
            status=False,
        )
    )
    assert (tmp_path / "state/postings.json").read_bytes() == before
    assert jsonfile.read(tmp_path / "state/descriptions.json")["acme:R1"]["status"] == "ready"


def test_dry_run_does_not_create_cache_or_contact_sources(cfg, tmp_path, monkeypatch):
    state.save(State(postings={"acme:R1": posting()}), tmp_path / "state")
    monkeypatch.setattr(cli.http, "client", lambda: pytest.fail("No HTTP in dry run"))
    cli.cmd_collect_descriptions(
        argparse.Namespace(
            config=tmp_path / "config.toml",
            data_dir=tmp_path,
            state_dir=tmp_path / "state",
            limit=1,
            max_seconds=10,
            company=None,
            dry_run=True,
            status=False,
        )
    )
    assert not (tmp_path / "state/descriptions.json").exists()


def test_saved_prisma_and_phenom_descriptions_include_real_source_text():
    for fixture, title in [
        ("description_prisma.json", "Pediatric Polysomnographic Technologist PT, Nights"),
        ("description_phenom.json", "Physician"),
    ]:
        data = json.loads(fixture_text(fixture))
        source = '<script type="application/ld+json">' + json.dumps(data) + "</script>"
        text, _ = descriptions.parse_html(source, posting(title=title))
        assert len(text) > 200
        assert "<p>" not in text and "&lt;" not in text


@pytest.mark.parametrize(
    "fixture,title",
    [
        ("description_successfactors.html", "CNC Machine Operator - Night Shift - Pendleton"),
        ("description_michelin.html", "Quality Process Engineer"),
    ],
)
def test_saved_html_descriptions(fixture, title):
    text, _ = descriptions.parse_html(fixture_text(fixture), posting(title=title))
    assert len(text) > 5000


def test_scheduled_collection_waits_for_opt_in(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(cli.http, "client", lambda: pytest.fail("Not enabled"))
    descriptions.run([posting()], cfg, tmp_path / "descriptions.json")


def test_scheduled_collection_failure_does_not_fail_crawl(cfg, tmp_path, monkeypatch, caplog):
    path = tmp_path / "descriptions.json"
    jsonfile.write(path, {})

    def broken(*args):
        raise RuntimeError("Collector interrupted")

    monkeypatch.setattr(descriptions, "collect", broken)
    descriptions.run([posting()], cfg, path)
    assert "completed records remain saved" in caplog.text
