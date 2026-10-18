import copy
import json
from dataclasses import replace

import httpx
import pytest
from test_classify import ACME, PROFILE, posting

from jobscraper.classification import jev
from jobscraper.config import Config, Notifications

CFG = Config(PROFILE, Notifications(""), list(ACME.values()))


def job(key="a:1", relevance="core", **kwargs):
    return posting(
        key,
        "Software developer",
        classification={
            "relevance": relevance,
            "model": "claude-test",
            "reason": "SECRET BASELINE REASON",
            "rules": "old",
            "level": "mid",
            "score": 10,
        },
        **kwargs,
    )


def response(choice="core"):
    return {
        "model": jev.MODEL,
        "answers": {
            "relevance": {
                "type": "choice",
                "choice": choice,
                "confidence": 0.9,
                "probabilities": {k: float(k == choice) for k in jev.CRITERIA},
            }
        },
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }


def test_selection_balances_labels_and_excludes_non_ai_duplicates_removed():
    jobs = [
        job("a:1", "none"),
        job("a:2", "none"),
        job("a:3", "core"),
        job("a:4", "adjacent"),
        job("a:5", "maybe"),
        job("a:6", status="removed"),
        job("a:7", duplicate_of="a:3"),
        job("a:10", "industrial"),
    ]
    jobs.append(replace(job("a:8"), classification={"model": "keywords", "relevance": "core"}))
    jobs.append(replace(job("a:9"), classification=None))
    data = {p.key: p for p in jobs}
    assert [p.classification["relevance"] for p in jev.select_postings(data, 5, False)] == list(
        jev.CRITERIA
    )
    assert len(jev.select_postings(data, 0, False)) == 6
    assert len(jev.select_postings(data, 0, True)) == 7


def test_request_hides_baseline_and_limits_summary():
    body = jev.request_body(job(summary="x" * 500), CFG)
    assert "SECRET BASELINE" not in json.dumps(body)
    assert len(body["state"]["job"]["summary"]) == 300
    assert body["questions"]["relevance"]["criteria"] == jev.CRITERIA


def test_comparison_resumes_and_does_not_mutate_baseline(tmp_path):
    p = job()
    before = copy.deepcopy(p)
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=response("none"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = jev.compare([p], CFG, tmp_path, client)
        second = jev.compare([p], CFG, tmp_path, client)
        changed = jev.compare([replace(p, title="Developer II")], CFG, tmp_path, client)
    assert p == before
    assert first["potential_missed_matches"] == 1
    assert first["input_tokens_new_calls"] == 100
    assert second["new_calls"] == 0
    assert changed["new_calls"] == 1
    assert len(calls) == 2
    assert (tmp_path / "report.md").exists()


def test_partial_failure_preserves_completed_cache(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 2:
            return httpx.Response(401, text="secret must not be echoed")
        return httpx.Response(200, json=response())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="^Jev API returned HTTP 401$"):
            jev.compare([job(), replace(job("a:2"), title="Other")], CFG, tmp_path, client)
        report = jev.compare([job()], CFG, tmp_path, client)
    assert report["new_calls"] == 0
    assert len(calls) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"choice": "bogus"},
        {"confidence": float("nan")},
        {"probabilities": {"core": 1}},
        {"type": "noul"},
    ],
)
def test_invalid_answers_fail_closed(change):
    data = response()
    data["answers"]["relevance"].update(change)
    with pytest.raises(ValueError, match="invalid relevance"):
        jev.validate_response(data)


def test_rate_limits_retry(monkeypatch):
    sleeps = []
    monkeypatch.setattr(jev.time, "sleep", sleeps.append)
    responses = iter([httpx.Response(429), httpx.Response(200, json=response())])
    with httpx.Client(transport=httpx.MockTransport(lambda _: next(responses))) as client:
        assert jev.evaluate(client, {})["model"] == jev.MODEL
    assert sleeps == [2]


def test_shared_relevance_policy_and_historical_response_compatibility():
    from jobscraper import config
    from jobscraper.classification import classify, policy

    cfg = config.load()
    p = job()
    request = jev.request_body(p, cfg)
    assert request["questions"]["relevance"]["instructions"] in classify.SYSTEM
    assert request["questions"]["relevance"]["criteria"] == policy.CRITERIA
    assert request["state"]["profile"]["screening_rules"] in classify._prompt([p], cfg.profile, {})
    assert request["state"]["profile"]["rules_version"] == "software-fit-v2"
    old = response()
    old["answers"]["relevance"]["probabilities"].pop("industrial")
    assert jev.validate_response(old) == old
    with pytest.raises(ValueError):
        jev.validate_response(old, policy.CRITERIA)
    assert (
        jev.validate_response(response("industrial"))["answers"]["relevance"]["choice"]
        == "industrial"
    )
