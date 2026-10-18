from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from conftest import llm_result

from jobscraper.classification import classify
from jobscraper.config import Profile
from jobscraper.models import Company
from jobscraper.storage.state import Posting

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
        parsed = classify.Results(results=[llm_result(**r) for r in self.results])
        return SimpleNamespace(stop_reason=self.stop_reason, parsed_output=parsed)


def fake_client(results, **kw):
    return SimpleNamespace(messages=FakeMessages(results, **kw))


def test_llm_batches_postings_and_keeps_only_known_ids():
    postings = [posting("a:1", "Software Developer I"), posting("a:2", "Forklift Operator")]
    client = fake_client(
        [
            {
                "id": "a:1",
                "relevance": "core",
                "level": "entry",
                "reason": "dev role",
                "score": 10,
            },
            {
                "id": "a:2",
                "relevance": "none",
                "level": "unknown",
                "reason": "warehouse",
                "score": 1,
            },
            {
                "id": "zz:9",
                "relevance": "core",
                "level": "mid",
                "reason": "hallucinated",
                "score": 9,
            },
        ]
    )
    out = classify.classify_llm(postings, PROFILE, ACME, client)

    assert set(out) == {"a:1", "a:2"}
    assert out["a:1"]["relevance"] == "core"
    assert out["a:1"]["model"] == classify.DEFAULT_MODEL
    assert out["a:1"]["score"] == 10
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


def test_llm_isolates_the_posting_that_breaks_a_batch(monkeypatch):
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
        results = [llm_result(i, relevance="core", level="mid", score=9) for i in ids]
        return SimpleNamespace(
            stop_reason="end_turn", parsed_output=classify.Results(results=results)
        )

    client = SimpleNamespace(messages=SimpleNamespace(parse=parse))
    out = classify.classify_llm(postings, PROFILE, ACME, client)
    # [a0 a1] ok; [a2 a3] fails, splits into [a2] (fails) and [a3] (ok); [a4] ok
    assert len(calls) == 5
    assert set(out) == {"a:0", "a:1", "a:3", "a:4"}


def test_llm_stops_when_claude_is_unavailable(monkeypatch):
    import anthropic
    import httpx

    monkeypatch.setattr(classify, "BATCH_SIZE", 2)
    calls = []

    def parse(**kwargs):
        calls.append(kwargs)
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        raise anthropic.RateLimitError(
            "slow down", response=httpx.Response(429, request=request), body=None
        )

    client = SimpleNamespace(messages=SimpleNamespace(parse=parse))
    postings = [posting(f"a:{i}", "Dev") for i in range(6)]
    assert classify.classify_llm(postings, PROFILE, ACME, client) == {}
    assert len(calls) == 1  # no splitting, no further batches


@pytest.mark.parametrize(
    ("title", "relevance", "level"),
    [
        ("Software Developer I", "core", "entry"),
        ("Senior Software Engineer", "core", "senior"),
        ("IT Support Specialist", "adjacent", "unknown"),
        ("Summer 2027 Internship: Engineering and/or Sciences, IT/IS", "none", "intern"),
        ("Lead Data Analyst", "adjacent", "lead"),
        ("CNC Machine Operator", "none", "unknown"),
        ("Medical Device Technician", "none", "unknown"),  # "dev" mustn't match "Device"
        ("Development Program Coordinator", "none", "unknown"),
        ("Web Developers - Contract", "core", "unknown"),  # plurals still match
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
    display_only = Profile(**{**PROFILE.__dict__, "levels": ("senior",), "mode": "strict"})
    assert base == classify.rules_fingerprint(display_only, "m1")


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
        ("adjacent", "industrial", "entry", "industrial"),
        ("strict", "industrial", "mid", "industrial"),
        ("adjacent", "industrial", "senior", None),
    ],
)
def test_match_group(mode, relevance, level, expected):
    profile = Profile(target_role="dev", mode=mode, levels=("intern", "entry", "mid"))
    c = {"relevance": relevance, "level": level}
    assert classify.match_group(c, profile) == expected


def test_keyword_short_terms_match_whole_words_only():
    profile = Profile(target_role="software developer", core_examples=("dev", "SWE"))
    titles = {"a:1": "Sr. Dev (Python)", "a:2": "Medical Device Tech", "a:3": "SWE II"}
    out = classify.classify_keywords([posting(k, t) for k, t in titles.items()], profile)
    assert {k: v["relevance"] for k, v in out.items()} == {
        "a:1": "core",
        "a:2": "none",
        "a:3": "core",
    }


def test_policy_changes_invalidate_cached_assessments_and_are_stored():
    from dataclasses import replace

    base = classify.rules_fingerprint(PROFILE, "m")
    for field, value in [
        ("screening_rules", "No sales"),
        ("rules_version", "v2"),
        ("review_examples", ("robot programming",)),
    ]:
        profile = replace(PROFILE, **{field: value})
        assert classify.rules_fingerprint(profile, "m") != base
    profile = replace(PROFILE, rules_version="v2")
    p = posting("a:1", "Robot Programmer")
    client = fake_client([dict(id=p.key, relevance="industrial", level="mid", score=4)])
    result = classify.classify_batch([p], profile, ACME, client)[p.key]
    assert result["policy_version"] == "v2"
    assert result["relevance"] == "industrial"
    assert "salary" not in result
    assert (
        classify.posting_group(posting("a:2", "PLC programmer", classification=result), profile)
        == "industrial"
    )
    extended = posting("a:3", "PLC programmer", area="extended", classification=result)
    assert classify.posting_group(extended, profile) is None


def test_policy_keyword_fallback_does_not_promote_sales_interns_or_industrial():
    from jobscraper import config

    profile = config.load().profile
    jobs = [
        posting("a:1", "Robot Programmer"),
        posting("a:2", "Software Developer Intern"),
        posting("a:3", "Technical Sales Engineer"),
        posting("a:4", "Software Developer"),
    ]
    result = classify.classify_keywords(jobs, profile)
    assert [result[p.key]["relevance"] for p in jobs] == ["industrial", "none", "none", "core"]
