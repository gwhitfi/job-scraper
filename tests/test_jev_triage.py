import json
from dataclasses import replace

import httpx
import pytest
from test_classify import posting
from test_jev import CFG

from jobscraper.classification import jev, jev_triage, jev_v6
from jobscraper.scraping import descriptions


def description(p, text="Build software. Requires 0-2 years and Computer Science.", cfg=CFG):
    return {
        "status": "ready",
        "text": text,
        "signature": descriptions.signature(p, cfg.company(p.company_id)),
    }


def triage_response(kind="developer", level="entry", version="v6"):
    def answer(choice, criteria):
        return {
            "type": "choice",
            "choice": choice,
            "confidence": 0.9,
            "probabilities": {k: float(k == choice) for k in criteria},
        }

    data = {
        "model": jev.MODEL,
        "answers": {
            "kind": answer(kind, jev_triage.VERSIONS[version].kinds),
            "level": answer(level, jev_triage.VERSIONS[version].levels),
        },
        "usage": {"input_tokens": 700, "output_tokens": 100},
    }

    if version == "v6":
        for name, choice, criteria in (
            ("eligibility", "verify", jev_v6.ELIGIBILITY),
            ("degree", "unclear", jev_v6.DEGREES),
            ("fit", "reject", jev_v6.FITS),
            ("programming", "none", jev_v6.PROGRAMMING),
        ):
            data["answers"][name] = answer(choice, criteria)
    return data


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_collect_saves_answers_and_only_reasks_changed_jobs(tmp_path):
    path = tmp_path / "jev" / "triage-v6.json"
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=triage_response(version="v6"))

    jobs = [
        posting("a:1", "Developer", summary="one"),
        posting("a:2", "Help desk", status="removed"),
        posting("a:3", "Developer", duplicate_of="a:1"),
    ]
    with client(handler) as c:
        assert jev_triage.collect(jobs, CFG, path, c, version="v6") == 1
        assert jev_triage.collect(jobs, CFG, path, c, version="v6") == 0
        jobs[0] = replace(jobs[0], summary="changed")
        assert jev_triage.collect(jobs, CFG, path, c, version="v6") == 1
    saved = json.loads(path.read_text())
    assert list(saved) == ["a:1"]
    assert saved["a:1"]["request"]["state"]["job"]["summary"] == "changed"
    assert len(calls) == 2


def test_collect_stops_at_first_failure_and_keeps_finished(tmp_path):
    path = tmp_path / "triage-v6.json"
    answers = iter([httpx.Response(200, json=triage_response(version="v6")), httpx.Response(401)])
    jobs = [posting(f"a:{i}", "Developer") for i in range(3)]
    with client(lambda request: next(answers)) as c, pytest.raises(RuntimeError, match="401"):
        jev_triage.collect(jobs, CFG, path, c, version="v6")
    assert len(json.loads(path.read_text())) == 1


def test_missing_level_answer_is_rejected(tmp_path):
    bad = triage_response(version="v6")
    del bad["answers"]["level"]
    with (
        client(lambda request: httpx.Response(200, json=bad)) as c,
        pytest.raises(RuntimeError, match="invalid level"),
    ):
        jev_triage.collect([posting("a:1", "Developer")], CFG, tmp_path / "t.json", c)


@pytest.mark.parametrize("problem", ["missing", "blocked", "stale"])
def test_missing_failed_or_mismatched_full_text_is_never_sent_as_complete(problem):
    p = posting("a:1", "Developer", summary="x" * 2500)
    cached = description(p, "Old cached full text")
    if problem == "missing":
        cached = None
    elif problem == "blocked":
        cached["status"] = "blocked"
    else:
        cached["signature"] = "old-inputs"
    body = jev_triage.request_body(p, CFG, "v6", description=cached)
    assert body["state"]["job"]["description"] is None
    assert body["state"]["job"]["description_complete"] is False
    assert len(body["state"]["job"]["summary"]) == 2500


def test_only_v6_runs_after_crawl_even_when_legacy_caches_exist(tmp_path, monkeypatch):
    calls = []

    def fake(postings, cfg, path, client, version):
        calls.append(version)
        raise RuntimeError("Jev API returned HTTP 500")

    monkeypatch.setattr(jev_triage, "collect", fake)
    for name in (
        "triage.json",
        "triage-v2.json",
        "triage-v3.json",
        "triage-v4.json",
        "triage-v5.json",
    ):
        (tmp_path / name).write_text("{}")
    jev_triage.run([posting("a:1", "Developer")], CFG, tmp_path, "key")
    assert calls == []
    jev_triage.store_path(tmp_path, "v6").write_text("{}")
    jev_triage.run([posting("a:1", "Developer")], CFG, tmp_path, "key")
    assert calls == ["v6"]


def test_disabled_sources_are_not_evaluated():
    cfg = replace(CFG, companies=tuple(replace(c, enabled=False) for c in CFG.companies))
    assert jev_triage.pending([posting("a:1", "Developer")], cfg, {}) == []
