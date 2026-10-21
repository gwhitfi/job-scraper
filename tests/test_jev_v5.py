import json
from dataclasses import replace

import httpx
import pytest
from test_classify import posting
from test_jev import CFG
from test_jev_triage import triage_response

from jobscraper.classification import jev_triage, jev_v5
from jobscraper.scraping import descriptions
from jobscraper.storage import jsonfile


def v5_response(kind="developer", level="entry", degree="cs_listed", fit="match"):
    response = triage_response(kind, level, "v5")
    for name, choice, criteria in (("degree", degree, jev_v5.DEGREES), ("fit", fit, jev_v5.FITS)):
        response["answers"][name] = {
            "type": "choice",
            "choice": choice,
            "confidence": 0.9,
            "probabilities": {k: float(k == choice) for k in criteria},
        }
    return response


def description(p, text="Build software. Requires 0-2 years and Computer Science.", cfg=CFG):
    return {
        "status": "ready",
        "text": text,
        "signature": descriptions.signature(p, cfg.company(p.company_id)),
    }


def test_full_description_is_untruncated_and_has_no_baseline_or_marks():
    p = posting("a:1", "Developer", summary="short", classification={"reason": "SECRET BASELINE"})
    text = "Build software. " * 600 + "Required: 2 years. Preferred: Computer Science degree."
    body = jev_triage.request_body(p, CFG, description=description(p, text))
    assert body["state"]["job"]["description"] == text
    assert body["state"]["job"]["description_complete"] is True
    assert "first job" in body["state"]["profile"]
    assert "SECRET BASELINE" not in json.dumps(body)
    assert set(body["questions"]) == {"kind", "level", "degree", "fit"}
    assert jev_triage.LATEST == "v5"


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
    body = jev_triage.request_body(p, CFG, description=cached)
    assert body["state"]["job"]["description"] is None
    assert body["state"]["job"]["description_complete"] is False
    assert len(body["state"]["job"]["summary"]) == 2500


@pytest.mark.parametrize(
    "kind,level,degree,fit,complete,expected",
    [
        ("developer", "entry", "cs_listed", "match", True, "exact"),
        ("developer", "entry", "unspecified", "match", True, "exact"),
        ("developer", "entry", "excluded", "evaluate", True, None),
        ("developer", "entry", "excluded", "match", True, None),
        ("developer", "entry", "unclear", "match", True, "evaluate"),
        ("developer", "entry", "unspecified", "match", False, "evaluate"),
        ("developer", "entry", "cs_listed", "match", False, "evaluate"),
        ("developer", "unsure", "cs_listed", "match", True, "evaluate"),
        ("developer", "mid", "cs_listed", "evaluate", True, "evaluate"),
        ("developer", "senior", "cs_listed", "evaluate", True, None),
        ("developer", "internship", "cs_listed", "evaluate", True, None),
        ("developer", "entry", "cs_listed", "reject", True, None),
        ("code_adjacent", "entry", "cs_listed", "match", True, "evaluate"),
        ("industrial_automation", "entry", "cs_listed", "match", True, None),
        ("helpdesk", "entry", "cs_listed", "evaluate", True, None),
        ("application_support", "entry", "cs_listed", "evaluate", True, "evaluate"),
        ("data_analysis", "entry", "cs_listed", "evaluate", True, "evaluate"),
        ("it_infrastructure", "entry", "cs_listed", "evaluate", True, "evaluate"),
        ("business_tech", "entry", "cs_listed", "evaluate", True, None),
        ("other", "entry", "unspecified", "match", True, None),
    ],
)
def test_v5_fit_gates(kind, level, degree, fit, complete, expected):
    record = {
        "request": {"state": {"job": {"description_complete": complete}}},
        "response": v5_response(kind, level, degree, fit),
    }
    assert jev_triage.group(record, "primary", "v5") == expected
    assert jev_triage.group(record, "extended", "v5") == (expected if expected == "exact" else None)


def test_low_confidence_match_is_evaluate():
    response = v5_response()
    response["answers"]["degree"]["confidence"] = 0.5
    record = {"request": {"state": {"job": {"description_complete": True}}}, "response": response}
    assert jev_triage.group(record, "primary", "v5") == "evaluate"


def test_collect_resumes_and_reasks_only_when_description_content_changes(tmp_path):
    p = posting("a:1", "Developer")
    path = jev_triage.store_path(tmp_path / "jev", "v5")
    previous = jev_triage.store_path(path.parent, "v4")
    jsonfile.write(previous, {p.key: {"old": True}})
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=v5_response())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert jev_triage.collect([p], CFG, path, client, descriptions={}) == 1
        assert jev_triage.collect([p], CFG, path, client, descriptions={}) == 0
        cached = {p.key: description(p)}
        assert jev_triage.collect([p], CFG, path, client, descriptions=cached) == 1
        cached[p.key]["checked_at"] = "a later freshness check"
        assert jev_triage.collect([p], CFG, path, client, descriptions=cached) == 0
        cached[p.key]["text"] += " Preferred: 3 years."
        assert jev_triage.collect([p], CFG, path, client, descriptions=cached) == 1
    assert len(calls) == 3
    assert jsonfile.read(previous) == {p.key: {"old": True}}


def test_disabled_sources_are_not_evaluated():
    cfg = replace(CFG, companies=[replace(c, enabled=False) for c in CFG.companies])
    assert jev_triage.pending([posting("a:1", "Developer")], cfg, {}, "v5") == []


def test_all_four_answers_are_validated_before_saving(tmp_path):
    response = v5_response()
    del response["answers"]["degree"]
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json=response))
        ) as c,
        pytest.raises(RuntimeError, match="invalid degree"),
    ):
        jev_triage.collect([posting("a:1", "Developer")], CFG, tmp_path / "v5.json", c)
    assert jsonfile.read(tmp_path / "v5.json") == {}
