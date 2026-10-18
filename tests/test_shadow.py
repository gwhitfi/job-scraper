import copy
from dataclasses import replace

import httpx
from test_jev import CFG, job, response

from jobscraper.classification import shadow
from jobscraper.storage import jsonfile


def test_collect_keeps_original_inputs_and_does_not_repeat_completed_reviews(tmp_path):
    p = job(summary="Original duties")
    before = copy.deepcopy(p)
    calls = []

    def handler(request):
        calls.append(request)
        assert b"SECRET BASELINE" not in request.content
        return httpx.Response(200, json=response("none"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        shadow.collect([p], CFG, tmp_path, client)
        shadow.collect([p], CFG, tmp_path, client)
        shadow.collect(
            [replace(p, classification={**p.classification, "classified_at": "new"})],
            CFG,
            tmp_path,
            client,
        )
    assert p == before
    assert len(calls) == 2
    records = list(jsonfile.read(tmp_path / "records.json").values())
    assert len(records) == 2
    assert records[0]["request"]["state"]["job"]["summary"] == "Original duties"
    assert all(r["response"]["answers"]["relevance"]["choice"] == "none" for r in records)
    report = (tmp_path / "README.md").read_text()
    assert "DISAGREE" in report and "Original duties" in report
    assert "SECRET BASELINE REASON" in report


def test_outage_saves_queue_and_retries_with_original_profile(tmp_path):
    jobs = [job(), replace(job("a:2"), title="Second job")]
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, text="Do not print secrets")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        shadow.collect(jobs, CFG, tmp_path, client)
    records = jsonfile.read(tmp_path / "records.json")
    assert len(calls) == 1
    assert len(records) == 2
    assert "Do not print secrets" not in (tmp_path / "README.md").read_text()
    changed_cfg = replace(CFG, profile=replace(CFG.profile, target_role="changed"))

    def succeeds(request):
        assert b'"target_role":"software developer"' in request.content
        return httpx.Response(200, json=response())

    with httpx.Client(transport=httpx.MockTransport(succeeds)) as client:
        shadow.collect([], changed_cfg, tmp_path, client)
    assert all(r.get("response") for r in jsonfile.read(tmp_path / "records.json").values())


def test_missing_key_queues_but_keywords_are_not_ai_reviews(tmp_path):
    keyword = replace(job("a:2"), classification={"model": "keywords", "relevance": "core"})
    shadow.collect([job(), keyword], CFG, tmp_path, None)
    records = jsonfile.read(tmp_path / "records.json")
    assert len(records) == 1
    assert not next(iter(records.values())).get("response")
    assert "Awaiting Jev: **1**" in (tmp_path / "README.md").read_text()


def test_shadow_storage_errors_do_not_fail_the_crawl(tmp_path, monkeypatch):
    path = tmp_path / "not-a-directory"
    path.write_text("blocked")
    monkeypatch.setattr(shadow, "DEFAULT_DIR", path)
    shadow.run([job()], CFG, "")


def test_new_policy_cohorts_preserve_old_records(tmp_path):
    p = job()
    shadow.collect([p], CFG, tmp_path, None)
    cfg = replace(CFG, profile=replace(CFG.profile, rules_version="software-fit-v2"))
    industrial = replace(
        p,
        classification={
            **p.classification,
            "relevance": "industrial",
            "policy_version": "software-fit-v2",
        },
    )
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=response("industrial"))
        )
    ) as client:
        shadow.collect([industrial], cfg, tmp_path, client)
    records = jsonfile.read(tmp_path / "records.json")
    assert len(records) == 2
    assert {r["policy_version"] for r in records.values()} == {"legacy-v1", "software-fit-v2"}
    report = (tmp_path / "README.md").read_text()
    assert "legacy-v1" in report and "software-fit-v2" in report
    assert "Industrial / controls (manual review)" in report
