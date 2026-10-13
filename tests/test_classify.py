from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from jobscraper import classify
from jobscraper.config import Profile
from jobscraper.models import Company
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


def company(cid: str, name: str, about: str = "") -> Company:
    return Company(id=cid, name=name, adapter="x", url="u", added=date(2026, 1, 1), about=about)


ACME = {"a": company("a", "Acme")}


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
    out = classify.classify_llm(postings, PROFILE, ACME, client)

    assert set(out) == {"a:1", "a:2"}
    assert out["a:1"]["relevance"] == "core"
    assert out["a:1"]["model"] == classify.DEFAULT_MODEL
    (call,) = client.messages.calls
    prompt = call["messages"][0]["content"]
    assert "id=a:1 | Software Developer I | Acme" in prompt
    assert "IT support" in prompt


def test_prompt_includes_category_and_trimmed_summary():
    p = posting("a:1", "Engineer II - QA", category="Engineering", summary="x" * 1000)
    prompt = classify._prompt([p], PROFILE, {"a": company("a", "Acme", "Medical device maker")})
    assert "| Acme (Medical device maker) |" in prompt
    assert "| category: Engineering |" in prompt
    assert prompt.endswith("summary: " + "x" * classify.SUMMARY_CHARS)


def test_board_postings_labeled_with_employer():
    p = posting("b:1", "BI Developer", employer="AFL")
    board = {"b": company("b", "Move Upstate SC", "Regional job board")}
    assert "| AFL (via Move Upstate SC) |" in classify._prompt([p], PROFILE, board)


def test_batch_raises_when_truncated():
    client = fake_client([], stop_reason="max_tokens")
    with pytest.raises(RuntimeError):
        classify.classify_batch([posting("a:1", "Dev")], PROFILE, ACME, client)


def test_llm_splits_into_batches_and_skips_failed_ones(monkeypatch):
    monkeypatch.setattr(classify, "BATCH_SIZE", 2)
    postings = [posting(f"a:{i}", "Dev") for i in range(5)]
    calls = []

    def parse(**kwargs):
        calls.append(kwargs)
        ids = [
            line.split("id=")[1].split(" |")[0]
            for line in kwargs["messages"][0]["content"].splitlines()
            if line.startswith("- id=")
        ]
        if "a:2" in ids:
            raise RuntimeError("down")
        results = [classify.Result(id=i, relevance="core", level="mid", reason="r") for i in ids]
        return SimpleNamespace(
            stop_reason="end_turn", parsed_output=classify.Results(results=results)
        )

    client = SimpleNamespace(messages=SimpleNamespace(parse=parse))
    out = classify.classify_llm(postings, PROFILE, ACME, client)
    assert len(calls) == 3
    assert set(out) == {"a:0", "a:1", "a:4"}  # the a:2/a:3 batch failed


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
    rules = "abc"
    llm = {"model": "claude-sonnet-5-5", "rules": rules}
    assert classify.needs_classification(posting("a:1", "x"), rules)
    assert classify.needs_classification(
        posting("a:1", "x", classification={"model": classify.KEYWORD_MODEL}), rules
    )
    assert not classify.needs_classification(posting("a:1", "x", classification=llm), rules)
    assert classify.needs_classification(posting("a:1", "x", classification=llm), "changed")
    assert not classify.needs_classification(posting("a:1", "x", status="removed"), rules)


def test_rules_fingerprint_tracks_profile_and_model():
    base = classify.rules_fingerprint(PROFILE, "m1")
    assert base == classify.rules_fingerprint(PROFILE, "m1")
    assert base != classify.rules_fingerprint(PROFILE, "m2")
    changed = Profile(**{**PROFILE.__dict__, "core_examples": ("programmer",)})
    assert base != classify.rules_fingerprint(changed, "m1")


def test_prompt_lists_equivalent_titles():
    profile = Profile(target_role="software developer", core_examples=("programmer", "dev"))
    assert "Equivalent titles: programmer, dev" in classify._prompt([], profile, {})


@pytest.mark.parametrize(
    ("mode", "relevance", "level", "expected"),
    [
        ("adjacent", "core", "entry", "exact"),
        ("adjacent", "adjacent", "mid", "near"),
        ("adjacent", "maybe", "mid", "maybe"),
        ("strict", "core", "mid", "exact"),
        ("strict", "adjacent", "mid", None),
        ("strict", "maybe", "mid", None),
        ("adjacent", "core", "senior", None),
        ("adjacent", "maybe", "senior", None),
        ("adjacent", "core", "unknown", "exact"),
        ("adjacent", "none", "entry", None),
    ],
)
def test_match_group(mode, relevance, level, expected):
    profile = Profile(target_role="dev", mode=mode, levels=("intern", "entry", "mid"))
    c = {"relevance": relevance, "level": level}
    assert classify.match_group(c, profile) == expected
    assert classify.is_relevant(c, profile) is (expected is not None)
