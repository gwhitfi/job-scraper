from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from conftest import llm_result

from jobscraper import adapters, classify, runner, tracker
from jobscraper.config import Config, Notifications, Profile
from jobscraper.models import Company, RawPosting
from jobscraper.state import State

T0 = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
PROFILE = Profile(
    target_role="software developer",
    adjacent_examples=("data analyst",),
    levels=("intern", "entry", "mid"),
    keywords=("software", "developer"),
)


def company(cid: str) -> Company:
    return Company(id=cid, name=cid.title(), adapter=f"fake_{cid}", url="u", added=date(2026, 1, 1))


def cfg(*ids: str) -> Config:
    return Config(PROFILE, Notifications(email="a@b.c"), [company(i) for i in ids])


def quiet(d) -> bool:
    """Nothing new to report; the still-available reminder doesn't count."""
    return not (d.new_exact or d.new_near or d.new_maybe or d.removed or d.health)


def raw(sid: str, title: str) -> RawPosting:
    return RawPosting(source_id=sid, title=title, location="SC", url=f"https://x/{sid}")


@pytest.fixture
def fake_sites(monkeypatch):
    """Map company id -> list of postings, or an exception to raise."""
    sites: dict[str, object] = {}

    def make(cid):
        def adapter(company, client):
            value = sites[cid]
            if isinstance(value, Exception):
                raise value
            return value

        return adapter

    for cid in ("acme", "beta"):
        monkeypatch.setitem(adapters.ADAPTERS, f"fake_{cid}", make(cid))
    return sites


def do_run(c: Config, s: State, hours: int = 0, llm=None) -> runner.RunResult:
    return runner.run(
        c, s, httpx.Client(), llm, now=T0 + timedelta(hours=hours), sleep=lambda _: None
    )


def test_new_relevant_jobs_reported_irrelevant_ones_stored(fake_sites):
    fake_sites["acme"] = [raw("1", "Software Developer I"), raw("2", "Forklift Operator")]
    s = State()
    result = do_run(cfg("acme"), s)
    assert [p.key for p in result.digest.new_exact] == ["acme:1"]
    assert s.postings["acme:2"].classification["relevance"] == "none"
    assert result.fetched == {"acme": 2}


def test_second_identical_run_is_quiet(fake_sites):
    fake_sites["acme"] = [raw("1", "Software Developer I")]
    s = State()
    do_run(cfg("acme"), s)
    assert quiet(do_run(cfg("acme"), s, hours=1).digest)


def test_removed_relevant_job_reported_after_two_runs(fake_sites):
    fake_sites["acme"] = [raw("1", "Software Developer I"), raw("2", "Forklift Operator")]
    s = State()
    do_run(cfg("acme"), s)
    fake_sites["acme"] = [raw("9", "Janitor")]
    assert do_run(cfg("acme"), s, hours=1).digest.removed == []
    removed = do_run(cfg("acme"), s, hours=2).digest.removed
    assert [p.key for p in removed] == ["acme:1"]  # the forklift job is removed silently


def test_failure_isolated_retried_and_alerted_once(fake_sites):
    fake_sites["acme"] = [raw("1", "Software Developer I")]
    fake_sites["beta"] = httpx.ConnectError("boom")
    s = State()
    events = []
    for h in range(4):
        result = do_run(cfg("acme", "beta"), s, hours=h)
        assert "beta" in result.failed and "acme" in result.fetched
        events.append([e.kind for e in result.digest.health])
    assert events == [[], [], ["failing"], []]

    fake_sites["beta"] = [raw("7", "Data Analyst")]
    result = do_run(cfg("acme", "beta"), s, hours=5)
    assert [e.kind for e in result.digest.health] == ["recovered"]
    assert [p.key for p in result.digest.new_near] == ["beta:7"]


def test_suspicious_empty_does_not_remove(fake_sites):
    fake_sites["acme"] = [raw("1", "Software Developer I")]
    s = State()
    do_run(cfg("acme"), s)
    fake_sites["acme"] = []
    for h in (1, 2, 3):
        result = do_run(cfg("acme"), s, hours=h)
        assert "acme" in result.failed
    assert s.postings["acme:1"].status == "active"


def test_disabled_company_skipped(fake_sites):
    c = cfg("acme")
    c.companies[0] = Company(**{**c.companies[0].__dict__, "enabled": False})
    result = do_run(c, State())
    assert result.fetched == {} and result.failed == {}


class FailingLLM:
    class messages:  # noqa: N801
        @staticmethod
        def parse(**kwargs):
            raise RuntimeError("down")


class StubLLM:
    """Gives every posting the same verdict and counts calls."""

    def __init__(self, relevance: str = "core"):
        self.calls = 0
        stub = self

        class Messages:
            @staticmethod
            def parse(**kwargs):
                stub.calls += 1
                ids = [
                    line.split("id=")[1].split(" |")[0]
                    for line in kwargs["messages"][0]["content"].splitlines()
                    if line.startswith("- id=")
                ]
                results = [
                    llm_result(i, relevance=relevance, level="entry", reason="stub", score=8)
                    for i in ids
                ]
                return SimpleNamespace(
                    stop_reason="end_turn", parsed_output=classify.Results(results=results)
                )

        self.messages = Messages()


def test_llm_failure_falls_back_then_retry_upgrades(fake_sites):
    fake_sites["acme"] = [raw("1", "Platform Engineer")]  # no keyword hit
    s = State()
    result = do_run(cfg("acme"), s, llm=FailingLLM())
    assert quiet(result.digest)
    assert s.postings["acme:1"].classification["model"] == classify.KEYWORD_MODEL

    result = do_run(cfg("acme"), s, hours=1, llm=StubLLM())
    assert [p.key for p in result.digest.new_exact] == ["acme:1"]
    assert s.postings["acme:1"].classification["model"] == classify.DEFAULT_MODEL
    # Classified by the LLM now, so it isn't sent again.
    assert quiet(do_run(cfg("acme"), s, hours=2, llm=StubLLM()).digest)


def test_still_available_lists_older_exact_matches(fake_sites):
    fake_sites["acme"] = [raw("1", "Software Developer I")]
    s = State()
    first = do_run(cfg("acme"), s, llm=StubLLM())
    assert [p.key for p in first.digest.new_exact] == ["acme:1"]
    assert first.digest.still_available == []  # already listed as new

    fake_sites["acme"] = [raw("1", "Software Developer I"), raw("2", "Web Developer")]
    second = do_run(cfg("acme"), s, hours=1, llm=StubLLM())
    assert [p.key for p in second.digest.new_exact] == ["acme:2"]
    assert [p.key for p in second.digest.still_available] == ["acme:1"]


def test_rule_change_reclassifies_once_without_announcing(fake_sites):
    fake_sites["acme"] = [raw("1", "Platform Engineer")]
    s = State()
    do_run(cfg("acme"), s, llm=StubLLM("none"))

    changed = cfg("acme")
    changed = Config(
        Profile(**{**PROFILE.__dict__, "core_examples": ("platform engineer",)}),
        changed.notifications,
        changed.companies,
    )
    llm = StubLLM("core")
    result = do_run(changed, s, hours=1, llm=llm)
    assert llm.calls == 1
    assert s.postings["acme:1"].classification["relevance"] == "core"
    assert result.digest.new_exact == []
    assert [p.key for p in result.digest.still_available] == ["acme:1"]

    do_run(changed, s, hours=2, llm=llm)
    assert llm.calls == 1  # up to date now


def test_stale_llm_results_kept_without_llm(fake_sites):
    fake_sites["acme"] = [raw("1", "Platform Engineer")]
    s = State()
    do_run(cfg("acme"), s, llm=StubLLM("adjacent"))
    changed = Config(
        Profile(**{**PROFILE.__dict__, "mode": "strict"}),
        Notifications("a@b.c"),
        cfg("acme").companies,
    )
    for llm in (None, FailingLLM()):
        do_run(changed, s, hours=1, llm=llm)
        assert s.postings["acme:1"].classification["relevance"] == "adjacent"
        assert s.postings["acme:1"].classification["model"] == classify.DEFAULT_MODEL


def test_fetch_retries_then_succeeds(monkeypatch):
    calls = []

    def flaky(company, client):
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ReadTimeout("slow")
        return [raw("1", "x")]

    monkeypatch.setitem(adapters.ADAPTERS, "fake_acme", flaky)
    sleeps = []
    out = runner.fetch_with_retry(company("acme"), httpx.Client(), sleeps.append)
    assert len(out) == 1 and len(calls) == 3 and sleeps == [5.0, 15.0]


def test_fetch_company_tags_extended_area(monkeypatch):
    postings = [
        raw("1", "Dev"),  # location is just "SC": not a known Upstate town, but in SC
        RawPosting("2", "Dev", "Greenville, SC", "u"),
        RawPosting("3", "Dev", "Columbia, SC", "u"),
        RawPosting("4", "Dev", "Charlotte, NC", "u"),
        RawPosting("5", "Dev", "Austin, TX", "u"),
    ]
    monkeypatch.setitem(adapters.ADAPTERS, "fake_acme", lambda c, client: postings)
    c = Company(**{**company("acme").__dict__, "locations": ("upstate",)})
    regions = {"upstate": ("Greenville, SC",), "nc": ("Charlotte, NC",)}
    kept, dropped = runner.fetch_company(c, httpx.Client(), regions, extended=("SC", "nc"))
    areas = {p.source_id: p.area for p in kept}
    assert areas == {"1": "extended", "2": "primary", "3": "extended", "4": "extended"}
    assert [p.source_id for p in dropped] == ["5"]


def test_extended_area_only_announces_exact(fake_sites, monkeypatch):
    fake_sites["acme"] = [
        RawPosting("1", "Software Developer I", "Columbia, SC", "u"),
        RawPosting("2", "Data Analyst", "Columbia, SC", "u"),
    ]
    c = cfg("acme")
    c.companies[0] = Company(**{**c.companies[0].__dict__, "locations": ("upstate",)})
    c = Config(
        c.profile,
        c.notifications,
        c.companies,
        regions={"upstate": ("Greenville, SC",)},
        extended_locations=("SC",),
    )
    result = do_run(c, State())
    assert [p.key for p in result.digest.new_exact] == ["acme:1"]
    assert result.digest.new_near == []


def test_blocked_employers_left_out_and_unverified_labeled(fake_sites, monkeypatch):
    monkeypatch.setitem(adapters.ADAPTERS, "fake_board", lambda c, client: fake_sites["board"])
    fake_sites["board"] = [
        RawPosting("1", "Software Developer I", "SC", "u", employer="Shady LLC"),
        RawPosting("2", "Software Developer II", "SC", "u", employer="Blocked Inc"),
    ]
    monkeypatch.setattr(runner.employers, "is_board", lambda p, companies: True)
    c = Config(
        PROFILE,
        Notifications("a@b.c"),
        [Company(id="board", name="Board", adapter="fake_board", url="u", added=date(2026, 1, 1))],
    )

    llm = StubLLM("core")
    original = llm.messages.parse

    def parse(**kwargs):
        response = original(**kwargs)
        for r in response.parsed_output.results:
            r.employer_check = "verify"
        return response

    llm.messages.parse = parse
    decisions = {"employer:blocked": {"status": "blocked"}}
    result = runner.run(
        c, State(), httpx.Client(), llm, now=T0, sleep=lambda _: None, decisions=decisions
    )
    assert [p.key for p in result.digest.new_exact] == ["board:1"]
    assert result.digest.unverified == {"board:1"}


def test_unexpected_parser_error_only_fails_that_company(fake_sites, monkeypatch):
    def broken(company, client):
        raise AttributeError("'NoneType' object has no attribute 'get_text'")  # e.g. a redesign

    monkeypatch.setitem(adapters.ADAPTERS, "fake_beta", broken)
    fake_sites["acme"] = [raw("1", "Software Developer I")]
    result = do_run(cfg("acme", "beta"), State())
    assert "acme" in result.fetched
    assert result.failed["beta"].startswith("AttributeError")


def test_unexpected_classifier_error_falls_back_to_keywords(fake_sites):
    class WeirdLLM:
        class messages:  # noqa: N801
            @staticmethod
            def parse(**kwargs):
                raise TypeError("unexpected reply shape")

    fake_sites["acme"] = [raw("1", "Software Developer I")]
    s = State()
    result = do_run(cfg("acme"), s, llm=WeirdLLM())
    assert s.postings["acme:1"].classification["model"] == classify.KEYWORD_MODEL
    assert [p.key for p in result.digest.new_exact] == ["acme:1"]


def test_company_that_really_drops_to_zero_recovers(fake_sites):
    fake_sites["acme"] = [raw("1", "Software Developer I")]
    s = State()
    do_run(cfg("acme"), s)
    fake_sites["acme"] = []
    hour = 0
    for _ in range(tracker.EMPTY_RUNS_TO_ACCEPT - 1):
        hour += 2
        assert "acme" in do_run(cfg("acme"), s, hours=hour).failed
    hour += 2
    accepted = do_run(cfg("acme"), s, hours=hour)
    assert "acme" in accepted.fetched and [e.kind for e in accepted.digest.health] == ["recovered"]
    hour += 2
    assert [p.key for p in do_run(cfg("acme"), s, hours=hour).digest.removed] == ["acme:1"]
    assert "acme" in do_run(cfg("acme"), s, hours=hour + 2).fetched  # stays healthy at zero
