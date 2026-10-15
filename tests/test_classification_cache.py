from datetime import timedelta
from types import SimpleNamespace

import pytest
from conftest import llm_result
from test_classify import ACME, NOW, PROFILE, posting

from jobscraper import activity
from jobscraper.classification import classify, usage
from jobscraper.classification.classification_cache import Checkpoint
from jobscraper.storage import jsonfile


def client_with(parse):
    return SimpleNamespace(messages=SimpleNamespace(parse=parse))


def response_for(kwargs):
    ids = [
        line.split("id=")[1].split(" |")[0]
        for line in kwargs["messages"][0]["content"].splitlines()
        if line.startswith("- id=")
    ]
    return SimpleNamespace(
        stop_reason="end_turn",
        parsed_output=classify.Results(results=[llm_result(key, relevance="core") for key in ids]),
        usage=SimpleNamespace(input_tokens=100, output_tokens=50),
    )


def test_interrupted_classification_resumes_and_counts_usage_once(tmp_path, monkeypatch):
    monkeypatch.setattr(classify, "BATCH_SIZE", 1)
    path = tmp_path / "classification-cache.json"
    jobs = [posting("a:1", "Developer"), posting("a:2", "Analyst")]
    first = Checkpoint(path, "first", classify.DEFAULT_MODEL, NOW)
    calls = []

    def interrupt(**kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            raise KeyboardInterrupt()
        return response_for(kwargs)

    with pytest.raises(KeyboardInterrupt):
        classify.classify_llm(jobs, PROFILE, ACME, client_with(interrupt), checkpoint=first)
    assert set(jsonfile.read(path)["results"]) == {"a:1"}
    assert not (tmp_path / "postings.json").exists()

    second = Checkpoint(path, "second", classify.DEFAULT_MODEL, NOW + timedelta(hours=2))
    resumed = []

    def parse(**kwargs):
        resumed.append(kwargs)
        return response_for(kwargs)

    spent = usage.Usage()
    with activity.Trace(tmp_path / "crawl-history.json", "crawl") as trace:
        results = classify.classify_llm(
            jobs, PROFILE, ACME, client_with(parse), usage=spent, checkpoint=second
        )
    assert set(results) == {"a:1", "a:2"}
    assert len(resumed) == 1 and "id=a:2" in resumed[0]["messages"][0]["content"]
    assert any(
        e["stage"] == "classification_plan" and e["reused"] == 1 for e in trace.record["events"]
    )
    assert any(
        e["stage"] == "classification_progress" and e["completed"] == 2
        for e in trace.record["events"]
    )
    # Replaying checkpoints and final run accounting never bills either call twice.
    second.account()
    ledger = usage.record(tmp_path / "usage.json", spent, classify.DEFAULT_MODEL, NOW, "second")
    assert ledger["months"]["2026-10"]["calls"] == 2
    assert ledger["months"]["2026-10"]["input_tokens"] == 200
    second.finish()
    assert jsonfile.read(path)["results"] == {}


def test_prompt_changes_invalidate_cached_results(tmp_path):
    cache = Checkpoint(tmp_path / "cache.json", "run", classify.DEFAULT_MODEL, NOW)
    calls = []

    def parse(**kwargs):
        calls.append(kwargs)
        return response_for(kwargs)

    for title in ("Developer", "Developer", "Accountant"):
        classify.classify_llm(
            [posting("a:1", title)], PROFILE, ACME, client_with(parse), checkpoint=cache
        )
    assert len(calls) == 2


def test_checkpoint_failure_does_not_split_or_repeat_paid_request(tmp_path, monkeypatch):
    cache = Checkpoint(tmp_path / "cache.json", "run", classify.DEFAULT_MODEL, NOW)
    calls = []

    def parse(**kwargs):
        calls.append(kwargs)
        return response_for(kwargs)

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(cache, "save", fail)
    with pytest.raises(OSError, match="disk full"):
        classify.classify_llm(
            [posting("a:1", "Dev"), posting("a:2", "Dev")],
            PROFILE,
            ACME,
            client_with(parse),
            checkpoint=cache,
        )
    assert len(calls) == 1


def test_split_batch_keeps_completed_half_when_api_becomes_unavailable(tmp_path):
    import anthropic
    import httpx

    calls = []

    def parse(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("truncated batch")
        if len(calls) == 3:
            raise anthropic.RateLimitError(
                "slow down",
                response=httpx.Response(
                    429, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
                ),
                body=None,
            )
        return response_for(kwargs)

    cache = Checkpoint(tmp_path / "cache.json", "run", classify.DEFAULT_MODEL, NOW)
    result = classify.classify_llm(
        [posting("a:1", "Dev"), posting("a:2", "Dev")],
        PROFILE,
        ACME,
        client_with(parse),
        checkpoint=cache,
    )
    assert set(result) == {"a:1"}
    assert set(jsonfile.read(cache.path)["results"]) == {"a:1"}


def test_recovery_between_checkpoint_and_usage_ledger(tmp_path, monkeypatch):
    path = tmp_path / "cache.json"
    cache = Checkpoint(path, "first", classify.DEFAULT_MODEL, NOW)
    record = usage.record

    def fail(*args, **kwargs):
        raise OSError("ledger unavailable")

    monkeypatch.setattr(usage, "record", fail)
    with pytest.raises(OSError):
        classify.classify_llm(
            [posting("a:1", "Dev")],
            PROFILE,
            ACME,
            client_with(lambda **kwargs: response_for(kwargs)),
            checkpoint=cache,
        )
    assert "a:1" in jsonfile.read(path)["results"]
    monkeypatch.setattr(usage, "record", record)
    recovered = Checkpoint(path, "second", classify.DEFAULT_MODEL, NOW)
    result = classify.classify_llm(
        [posting("a:1", "Dev")],
        PROFILE,
        ACME,
        client_with(lambda **kwargs: pytest.fail("Paid result should be reused")),
        checkpoint=recovered,
    )
    assert "a:1" in result
    assert jsonfile.read(tmp_path / "usage.json")["months"]["2026-10"]["calls"] == 1


def test_finish_retains_paid_results_not_applied_to_state(tmp_path):
    cache = Checkpoint(tmp_path / "cache.json", "run", classify.DEFAULT_MODEL, NOW)
    cache.save({"a:1": {"model": "paid"}}, {"a:1": "signature"}, usage.Usage())
    cache.finish({"a:1": posting("a:1", "Dev", classification={"model": "keywords"})})
    assert "a:1" in jsonfile.read(cache.path)["results"]
