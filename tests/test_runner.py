from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from jobscraper import adapters, classify, runner
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
    assert [p.key for p in result.digest.new] == ["acme:1"]
    assert s.postings["acme:2"].classification["relevance"] == "none"
    assert result.fetched == {"acme": 2}


def test_second_identical_run_is_quiet(fake_sites):
    fake_sites["acme"] = [raw("1", "Software Developer I")]
    s = State()
    do_run(cfg("acme"), s)
    assert do_run(cfg("acme"), s, hours=1).digest.is_empty()


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
    assert [p.key for p in result.digest.new] == ["beta:7"]


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
    """Says every posting is a core entry-level job."""

    class messages:  # noqa: N801
        @staticmethod
        def parse(**kwargs):
            from types import SimpleNamespace

            ids = [
                line.split("id=")[1].split(" |")[0]
                for line in kwargs["messages"][0]["content"].splitlines()
                if line.startswith("- id=")
            ]
            results = [
                classify.Result(id=i, relevance="core", level="entry", reason="stub") for i in ids
            ]
            return SimpleNamespace(
                stop_reason="end_turn", parsed_output=classify.Results(results=results)
            )


def test_llm_failure_falls_back_then_retry_upgrades(fake_sites):
    fake_sites["acme"] = [raw("1", "Platform Engineer")]  # no keyword hit
    s = State()
    result = do_run(cfg("acme"), s, llm=FailingLLM())
    assert result.digest.new == []
    assert s.postings["acme:1"].classification["model"] == classify.KEYWORD_MODEL

    result = do_run(cfg("acme"), s, hours=1, llm=StubLLM())
    assert [p.key for p in result.digest.new] == ["acme:1"]
    assert s.postings["acme:1"].classification["model"] == classify.DEFAULT_MODEL
    # Classified by the LLM now, so it isn't sent again.
    assert do_run(cfg("acme"), s, hours=2, llm=StubLLM()).digest.is_empty()


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
