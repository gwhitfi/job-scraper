import json
from copy import deepcopy

import httpx
import pytest
from test_classify import posting
from test_jev import CFG
from test_jev_triage import description, triage_response
from test_web import app  # noqa: F401

from jobscraper.classification import jev_triage, jev_v6
from jobscraper.presentation import jev_v6 as page
from jobscraper.storage import jsonfile


def response(
    kind="developer",
    level="entry",
    degree="generic_or_none",
    fit="match",
    programming="primary",
    eligibility="clear",
):
    data = triage_response(kind, level, "v6")
    for name, choice, choices in (
        ("eligibility", eligibility, jev_v6.ELIGIBILITY),
        ("degree", degree, jev_v6.DEGREES),
        ("fit", fit, jev_v6.FITS),
        ("programming", programming, jev_v6.PROGRAMMING),
    ):
        data["answers"][name] = {
            "type": "choice",
            "choice": choice,
            "confidence": 0.9,
            "probabilities": {k: float(k == choice) for k in choices},
        }
    return data


def record(complete=True, **kwargs):
    return {
        "request": {"state": {"job": {"description_complete": complete}}},
        "response": response(**kwargs),
    }


@pytest.mark.parametrize("degree", ["cs_listed", "generic_or_none", "preferred_only"])
def test_compatible_required_or_preferred_degrees_can_match(degree):
    assert jev_v6.decision(record(degree=degree)).group == "exact"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"level": "senior"},
        {"level": "internship"},
        {"degree": "excluded"},
        {"kind": "industrial_automation"},
        {"kind": "sales_marketing"},
        {"kind": "other", "programming": "none", "fit": "reject"},
    ],
)
def test_confident_blockers_are_rejected_and_explained(kwargs):
    result = jev_v6.decision(record(**kwargs))
    assert result.group == "rejected" and result.reasons


@pytest.mark.parametrize(
    "question,choice",
    [
        ("kind", "other"),
        ("level", "senior"),
        ("degree", "excluded"),
        ("fit", "reject"),
    ],
)
def test_uncertain_negatives_preserve_plausible_programming_jobs(question, choice):
    r = record(**{question: choice})
    r["response"]["answers"][question]["confidence"] = 0.4
    assert jev_v6.decision(r).group == "evaluate"
    assert jev_triage.group(r, "extended", "v6") == "evaluate"


def test_contradictory_coding_evidence_is_never_a_strong_fit():
    assert jev_v6.decision(record(programming="none")).group == "evaluate"
    assert jev_v6.decision(record(fit="reject")).group == "evaluate"


def test_missing_description_and_experience_stretches_are_reviewed():
    assert jev_v6.decision(record(complete=False)).group == "evaluate"
    assert jev_v6.decision(record(level="mid")).group == "evaluate"
    assert jev_v6.decision(None).group == "pending"


def test_v6_request_has_complete_evidence_and_flexible_degree_policy():
    p = posting("a:1", "Developer")
    full = description(p, "Develop software. Generic degree preferred. No experience required.")
    body = jev_triage.request_body(p, CFG, "v6", description=full)
    assert body["state"]["job"]["description"] == full["text"]
    assert set(body["questions"]) == {
        "kind",
        "level",
        "degree",
        "fit",
        "programming",
        "eligibility",
    }
    assert "Preferred" in body["state"]["profile"]
    assert "preferred_only" in body["questions"]["degree"]["criteria"]


def test_v6_collect_validates_every_question_and_resumes(tmp_path):
    p = posting("a:1", "Developer")
    path = jev_triage.store_path(tmp_path, "v6")
    old = tmp_path / "triage-v5.json"
    jsonfile.write(old, {"untouched": True})
    data = response()
    with httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=data))
    ) as c:
        assert jev_triage.collect([p], CFG, path, c, version="v6", descriptions={}) == 1
        assert jev_triage.collect([p], CFG, path, c, version="v6", descriptions={}) == 0
        del data["answers"]["programming"]
        with pytest.raises(RuntimeError, match="programming"):
            jev_triage.collect(
                [p], CFG, path, c, version="v6", descriptions={p.key: description(p)}
            )
    assert jsonfile.read(old) == {"untouched": True}


def test_v6_page_pending_rejected_personal_and_escaping(app):  # noqa: F811
    p = app.state().postings["acme:1"]
    full = description(
        p, "<script>alert(1)</script> Build software. CS degree required.", cfg=app.cfg
    )
    body = jev_triage.request_body(p, app.cfg, "v6", description=full)
    r = {"request": body, "response": response(), "signature": jev_triage.signature(body)}
    jsonfile.write(app.state_dir / "descriptions.json", {p.key: full})
    jsonfile.write(jev_triage.store_path(app.jev_dir, "v6"), {p.key: r})
    html = app.index({"jev": "v6"}, "jev")
    assert "Job matches" in html and "Web Developer" in html
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "Pending assessment" in html and "Not a fit" in html
    app.mark(p.key, "starred", True)
    assert 'data-key="acme:1"' not in app.index({"jev": "v6"}, "jev")
    assert 'data-key="acme:1"' in app.index({"jev": "v6", "view": "starred"}, "jev")
    r["response"] = response(level="senior")
    jsonfile.write(jev_triage.store_path(app.jev_dir, "v6"), {p.key: r})
    app.mark(p.key, "starred", False)
    assert 'data-key="acme:1"' in app.index({"jev": "v6", "view": "rejected"}, "jev")
    changed = deepcopy(full)
    changed["text"] += " New mandatory qualification."
    jsonfile.write(app.state_dir / "descriptions.json", {p.key: changed})
    assert 'data-key="acme:1"' in app.index({"jev": "v6", "view": "pending"}, "jev")
    assert 'data-key="acme:1"' not in app.index({"jev": "v6", "view": "rejected"}, "jev")


def test_excerpts_are_labeled_and_do_not_invent_evidence():
    label, quotes = page.excerpts(
        {
            "request": {
                "state": {
                    "job": {
                        "description_complete": False,
                        "summary": "Software coding daily.",
                        "description": "Untrusted stale text.",
                    }
                }
            }
        }
    )
    assert label == "Summary excerpts" and quotes == ["Software coding daily."]


def test_uncertain_overall_fit_does_not_override_consistent_eligibility():
    r = record(fit="evaluate")
    r["response"]["answers"]["fit"]["confidence"] = 0.3
    assert jev_v6.decision(r).group == "exact"
    r["response"]["answers"]["eligibility"]["choice"] = "verify"
    assert jev_v6.decision(r).group == "evaluate"


def test_employer_gates_have_actions_and_numbered_titles_are_not_minimum_years(app):  # noqa: F811
    html = app.index({"jev": "v6", "view": "unverified"}, "jev")
    assert "Approve employer" in html and "Block employer" in html and "Clear decision" in html
    assert "level II" not in jev_triage.VERSIONS["v6"].levels["mid"]


def test_uncertainty_between_acceptable_degree_routes_is_not_a_mismatch():
    r = record(degree="generic_or_none")
    degree = r["response"]["answers"]["degree"]
    degree["confidence"] = 0.45
    degree["probabilities"] = dict.fromkeys(jev_v6.DEGREES, 0.0)
    degree["probabilities"].update(generic_or_none=0.45, preferred_only=0.45, unclear=0.1)
    assert jev_v6.decision(r).group == "exact"


def test_recorded_live_screening_cases():
    from conftest import fixture_text

    cases = json.loads(fixture_text("jev_v6_screening.json"))
    for case in cases:
        assert jev_v6.decision(case["record"]).group == case["expected"], case["title"]


def test_v6_request_matches_saved_production_prompt():
    from conftest import fixture_text

    cases = json.loads(fixture_text("jev_v6_screening.json"))
    saved = cases[0]["record"]["request"]
    p = posting("a:1", "Developer")
    current = jev_triage.request_body(p, CFG)
    assert current["questions"] == saved["questions"]
    assert current["state"]["profile"] == saved["state"]["profile"]
