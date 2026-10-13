from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from jobscraper import classify
from jobscraper.config import Profile
from jobscraper.state import Posting

NOW = datetime(2026, 10, 12, tzinfo=UTC)
PROFILE = Profile(
    target_role="software developer",
    mode="adjacent",
    adjacent_examples=("IT support", "data analyst", "QA/test"),
    exclude_examples=("product manager",),
    levels=("intern", "entry", "mid"),
    keywords=("software", "developer", "programming"),
)


def posting(key: str, title: str, **kw) -> Posting:
    company_id, source_id = key.split(":")
    return Posting(
        key=key,
        company_id=company_id,
        source_id=source_id,
        title=title,
        location="Greenville, SC",
        url="https://x",
        posted_date=None,
        first_seen=NOW,
        last_seen=NOW,
        **kw,
    )


class FakeMessages:
    def __init__(self, results, stop_reason="end_turn"):
        self.results = results
        self.stop_reason = stop_reason
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        parsed = classify.Results(results=[classify.Result(**r) for r in self.results])
        return SimpleNamespace(stop_reason=self.stop_reason, parsed_output=parsed)


def fake_client(results, **kw):
    return SimpleNamespace(messages=FakeMessages(results, **kw))


def test_llm_batches_postings_and_keeps_only_known_ids():
    postings = [posting("a:1", "Software Developer I"), posting("a:2", "Forklift Operator")]
    client = fake_client(
        [
            {"id": "a:1", "relevance": "core", "level": "entry", "reason": "dev role"},
            {"id": "a:2", "relevance": "none", "level": "unknown", "reason": "warehouse"},
            {"id": "zz:9", "relevance": "core", "level": "mid", "reason": "hallucinated"},
        ]
    )
    out = classify.classify_llm(postings, PROFILE, {"a": "Acme"}, client)

    assert set(out) == {"a:1", "a:2"}
    assert out["a:1"]["relevance"] == "core"
    assert out["a:1"]["model"] == classify.DEFAULT_MODEL
    (call,) = client.messages.calls
    prompt = call["messages"][0]["content"]
    assert "id=a:1 | Software Developer I | Acme" in prompt
    assert "IT support" in prompt


def test_prompt_includes_category_and_trimmed_summary():
    p = posting("a:1", "Engineer II - QA", category="Engineering", summary="x" * 1000)
    prompt = classify._prompt([p], PROFILE, {"a": "Acme (Medical device maker)"})
    assert "| Acme (Medical device maker) |" in prompt
    assert "| category: Engineering |" in prompt
    assert prompt.endswith("summary: " + "x" * classify.SUMMARY_CHARS)


def test_llm_raises_when_truncated():
    client = fake_client([], stop_reason="max_tokens")
    with pytest.raises(RuntimeError):
        classify.classify_llm([posting("a:1", "Dev")], PROFILE, {}, client)


@pytest.mark.parametrize(
    ("title", "relevance", "level"),
    [
        ("Software Developer I", "core", "entry"),
        ("Senior Software Engineer", "core", "senior"),
        ("IT Support Specialist", "adjacent", "unknown"),
        ("Summer 2027 Internship: Engineering and/or Sciences, IT/IS", "none", "intern"),
        ("Lead Data Analyst", "adjacent", "lead"),
        ("CNC Machine Operator", "none", "unknown"),
    ],
)
def test_keyword_fallback(title, relevance, level):
    out = classify.classify_keywords([posting("a:1", title)], PROFILE)["a:1"]
    assert (out["relevance"], out["level"]) == (relevance, level)
    assert out["model"] == classify.KEYWORD_MODEL


def test_needs_classification():
    assert classify.needs_classification(posting("a:1", "x"))
    assert classify.needs_classification(
        posting("a:1", "x", classification={"model": classify.KEYWORD_MODEL})
    )
    assert not classify.needs_classification(
        posting("a:1", "x", classification={"model": "claude-haiku-4-5"})
    )
    assert not classify.needs_classification(posting("a:1", "x", status="removed"))


@pytest.mark.parametrize(
    ("mode", "relevance", "level", "expected"),
    [
        ("adjacent", "core", "entry", True),
        ("adjacent", "adjacent", "mid", True),
        ("strict", "adjacent", "mid", False),
        ("adjacent", "core", "senior", False),
        ("adjacent", "core", "unknown", True),
        ("adjacent", "none", "entry", False),
    ],
)
def test_is_relevant(mode, relevance, level, expected):
    profile = Profile(target_role="dev", mode=mode, levels=("intern", "entry", "mid"))
    c = {"relevance": relevance, "level": level}
    assert classify.is_relevant(c, profile) is expected
