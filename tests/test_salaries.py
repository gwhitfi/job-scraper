from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_classify import ACME, PROFILE, posting

from jobscraper import config
from jobscraper.classification import classify, salaries
from jobscraper.storage import jsonfile, state


def estimate(**kw):
    return salaries.Salary(
        **(
            dict(minimum=65000, maximum=85000, period="year", currency="USD", confidence="medium")
            | kw
        )
    )


@pytest.mark.parametrize(
    "kw", [dict(minimum=-1), dict(minimum=90000), dict(maximum=float("inf")), dict(currency="CAD")]
)
def test_invalid_estimates_rejected(kw):
    with pytest.raises(ValidationError):
        estimate(**kw)


def test_salary_card_units_and_unknown():
    p = posting("a:1", "Developer")
    assert salaries.card(p) == ""
    p.salary = salaries.serialize(estimate(), "model")
    assert "$65,000–$85,000/year" in salaries.card(p)
    assert "AI-estimated pay" in salaries.card(p)
    assert "medium confidence" in salaries.card(p)
    p.salary = salaries.serialize(estimate(minimum=22.5, maximum=30, period="hour"), "model")
    assert "$22.50–$30/hour" in salaries.card(p)
    p.classification = {"salary": salaries.serialize(None, "model")}
    assert "insufficient information" in salaries.card(p)


def test_backfill_preserves_classification_and_is_resumable(tmp_path, monkeypatch):
    import anthropic

    cfg = config.Config(PROFILE, config.Notifications(email="x"), list(ACME.values()))
    monkeypatch.setattr(config, "load", lambda path: cfg)
    p = posting("a:1", "Developer")
    p.classification = {"model": "existing-model", "score": 8, "relevance": "core", "level": "mid"}
    s = state.State(postings={p.key: p})
    state.save(s, tmp_path / "state")
    calls = []

    class Client:
        def __enter__(self):
            self.messages = self
            return self

        def __exit__(self, *args):
            pass

        def parse(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                stop_reason="end_turn",
                usage=SimpleNamespace(input_tokens=100, output_tokens=50),
                parsed_output=salaries.SalaryResults(
                    results=[salaries.SalaryResult(id=p.key, salary=estimate())]
                ),
            )

    monkeypatch.setattr(anthropic, "Anthropic", Client)
    salaries.backfill(tmp_path)
    saved = state.load(tmp_path / "state").postings[p.key]
    assert saved.classification == p.classification
    assert saved.salary["estimate"]["minimum"] == 65000
    assert jsonfile.read(tmp_path / "salary-backfill.json")["status"] == "completed"
    salaries.backfill(tmp_path)
    assert len(calls) == 1


def test_classifier_returns_salary_in_same_review():
    from test_classify import fake_client

    p = posting("a:1", "Developer")
    client = fake_client([dict(id=p.key, relevance="core", level="mid", salary=estimate())])
    result = classify.classify_batch([p], PROFILE, ACME, client)
    assert result[p.key]["salary"]["estimate"]["maximum"] == 85000
    assert len(client.messages.calls) == 1
    assert salaries.INSTRUCTIONS in client.messages.calls[0]["system"]


@pytest.mark.parametrize(
    "relevance,level,area",
    [
        ("none", "mid", "primary"),
        ("core", "senior", "primary"),
        ("adjacent", "mid", "extended"),
    ],
)
def test_rejected_jobs_do_not_keep_model_salary(relevance, level, area):
    from test_classify import fake_client

    p = posting("a:1", "Billing Specialist", area=area)
    client = fake_client([dict(id=p.key, relevance=relevance, level=level, salary=estimate())])
    result = classify.classify_batch([p], PROFILE, ACME, client)
    assert "salary" not in result[p.key]
    assert "salary=null" in client.messages.calls[0]["system"]


def test_backfill_does_not_send_rejected_or_hidden_jobs(tmp_path, monkeypatch):
    import anthropic

    cfg = config.Config(PROFILE, config.Notifications(email="x"), list(ACME.values()))
    monkeypatch.setattr(config, "load", lambda path: cfg)
    rejected = posting(
        "a:1", "Billing Specialist", classification={"relevance": "none", "level": "mid"}
    )
    hidden = posting("a:2", "Developer", classification={"relevance": "core", "level": "mid"})
    state.save(state.State(postings={p.key: p for p in [rejected, hidden]}), tmp_path / "state")
    jsonfile.write(
        tmp_path / "marks.json", {hidden.key: {"hidden": True}, rejected.key: {"starred": True}}
    )

    def no_request():
        raise AssertionError("No salary request should be made for filtered-out jobs")

    monkeypatch.setattr(anthropic, "Anthropic", no_request)
    salaries.backfill(tmp_path)
    assert jsonfile.read(tmp_path / "salary-backfill.json")["total"] == 0
