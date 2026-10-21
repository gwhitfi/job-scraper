import json
from dataclasses import replace

import httpx
import pytest
from test_classify import posting
from test_jev import CFG

from jobscraper.classification import jev, jev_triage


def triage_response(kind="developer", level="entry", version="v1"):
    def answer(choice, criteria):
        return {
            "type": "choice",
            "choice": choice,
            "confidence": 0.9,
            "probabilities": {k: float(k == choice) for k in criteria},
        }

    return {
        "model": jev.MODEL,
        "answers": {
            "kind": answer(kind, jev_triage.VERSIONS[version].kinds),
            "level": answer(level, jev_triage.VERSIONS[version].levels),
        },
        "usage": {"input_tokens": 700, "output_tokens": 100},
    }


def record(kind, level, version="v1"):
    return {"response": triage_response(kind, level, version)}


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_request_sends_job_only_with_full_summary():
    p = posting("a:1", "Developer", summary="x" * 900, classification={"reason": "SECRET BASELINE"})
    body = jev_triage.request_body(p, CFG, "v4")
    text = json.dumps(body)
    assert "profile" not in body["state"]
    assert "SECRET BASELINE" not in text and CFG.profile.target_role not in text
    assert len(body["state"]["job"]["summary"]) == 900
    assert set(body["questions"]) == {"kind", "level"}


@pytest.mark.parametrize(
    ("kind", "level", "group"),
    [
        ("developer", "entry", "exact"),
        ("developer", "unsure", "exact"),
        ("developer", "mid", "maybe"),
        ("developer", "internship", "maybe"),
        ("developer", "senior", None),
        ("computing", "entry", "near"),
        ("computing", "unsure", "near"),
        ("computing", "mid", None),
        ("computing", "internship", None),
        ("helpdesk", "entry", "maybe"),
        ("helpdesk", "unsure", "maybe"),
        ("helpdesk", "mid", None),
        ("other", "entry", None),
    ],
)
def test_v1_groups(kind, level, group):
    assert jev_triage.group(record(kind, level), "primary", "v1") == group


@pytest.mark.parametrize(
    ("kind", "level", "group"),
    [
        ("developer", "entry", "exact"),
        ("developer", "unsure", "exact"),
        ("developer", "mid", "maybe"),
        ("developer", "internship", "maybe"),
        ("developer", "senior", None),
        ("code_adjacent", "entry", "near"),
        ("it_infrastructure", "unsure", "near"),
        ("data_analysis", "entry", "near"),
        ("data_analysis", "mid", None),
        ("helpdesk", "unsure", "maybe"),
        ("helpdesk", "mid", None),
        ("industrial_programming", "mid", "industrial"),
        ("industrial_programming", "internship", "industrial"),
        ("industrial_programming", "senior", None),
        ("business_tech", "entry", None),
        ("customer_service", "entry", None),
        ("general_application", "unsure", None),
        ("other", "entry", None),
    ],
)
def test_v2_groups(kind, level, group):
    assert jev_triage.group(record(kind, level, "v2"), "primary", "v2") == group


def test_every_v2_group_uses_real_choices():
    v2 = jev_triage.V2
    assert all(k in v2.kinds and lv in v2.levels for k, lv in v2.groups)
    assert set(v2.kind_labels) == set(v2.kinds) and set(v2.sort_kinds) <= set(v2.kinds)


def test_v2_level_rules_cover_title_numbers():
    text = jev_triage.V2.level_instructions
    assert "same in Roman or Arabic numerals" in text
    assert "II or 2 is mid, and III or 3\nand higher (IV, V, 4, 5) are senior" in text
    assert "(II or III, I-III) uses the lowest" in text
    assert "level III or 3 or higher" in jev_triage.V2.levels["senior"]


def test_v1_requests_are_unchanged_so_saved_answers_stay_valid():
    p = posting("a:1", "Developer", summary="Builds things")
    body = jev_triage.request_body(p, CFG, "v1")
    assert jev_triage.signature(body) == V1_SIGNATURE


def test_extended_area_only_keeps_exact_and_unknown_area_hides():
    assert jev_triage.group(record("developer", "entry"), "extended", "v1") == "exact"
    assert jev_triage.group(record("computing", "entry"), "extended", "v1") is None
    assert jev_triage.group(record("industrial_programming", "entry", "v2"), "extended") is None
    assert jev_triage.group(record("developer", "entry"), None, "v1") is None
    assert jev_triage.group(None, "primary") is None


# Saved v1 answers are matched by this; a change would silently re-ask every job.
V1_SIGNATURE = "047de2e9fd7d198c529d796da1aad1904c8c304ae69114554db81401868344a1"


def test_collect_saves_answers_and_only_reasks_changed_jobs(tmp_path):
    path = tmp_path / "jev" / "triage-v3.json"
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=triage_response(version="v3"))

    jobs = [
        posting("a:1", "Developer", summary="one"),
        posting("a:2", "Help desk", status="removed"),
        posting("a:3", "Developer", duplicate_of="a:1"),
    ]
    with client(handler) as c:
        assert jev_triage.collect(jobs, CFG, path, c, version="v3") == 1
        assert jev_triage.collect(jobs, CFG, path, c, version="v3") == 0
        jobs[0] = replace(jobs[0], summary="changed")
        assert jev_triage.collect(jobs, CFG, path, c, version="v3") == 1
    saved = json.loads(path.read_text())
    assert list(saved) == ["a:1"]
    assert saved["a:1"]["request"]["state"]["job"]["summary"] == "changed"
    assert len(calls) == 2


def test_versions_are_stored_and_asked_separately(tmp_path):
    def handler(request):
        version = "v2" if "code_adjacent" in request.content.decode() else "v1"
        return httpx.Response(200, json=triage_response(version=version))

    jobs = [posting("a:1", "Developer")]
    with client(handler) as c:
        for version in ("v1", "v2"):
            path = jev_triage.store_path(tmp_path, version)
            assert jev_triage.collect(jobs, CFG, path, c, version=version) == 1
    assert {p.name for p in tmp_path.glob("*.json")} == {"triage.json", "triage-v2.json"}


def test_collect_stops_at_first_failure_and_keeps_finished(tmp_path):
    path = tmp_path / "triage-v3.json"
    answers = iter([httpx.Response(200, json=triage_response(version="v3")), httpx.Response(401)])
    jobs = [posting(f"a:{i}", "Developer") for i in range(3)]
    with client(lambda request: next(answers)) as c, pytest.raises(RuntimeError, match="401"):
        jev_triage.collect(jobs, CFG, path, c, version="v3")
    assert len(json.loads(path.read_text())) == 1


def test_missing_level_answer_is_rejected(tmp_path):
    bad = triage_response(version="v3")
    del bad["answers"]["level"]
    with (
        client(lambda request: httpx.Response(200, json=bad)) as c,
        pytest.raises(RuntimeError, match="invalid level"),
    ):
        jev_triage.collect([posting("a:1", "Developer")], CFG, tmp_path / "t.json", c)


def test_crawl_hook_only_runs_started_versions_and_never_raises(tmp_path, monkeypatch):
    calls = []

    def fake(postings, cfg, path, client, version):
        calls.append(version)
        if version == "v1":
            raise RuntimeError("Jev API returned HTTP 500")

    monkeypatch.setattr(jev_triage, "collect", fake)
    jev_triage.run([posting("a:1", "Developer")], CFG, tmp_path, "key")
    assert calls == [] and not list(tmp_path.iterdir())

    for version in ("v1", "v2"):
        jev_triage.store_path(tmp_path, version).write_text("{}")
    jev_triage.run([posting("a:1", "Developer")], CFG, tmp_path, "key")
    assert calls == ["v1", "v2"]  # a v1 failure doesn't stop v2


@pytest.mark.parametrize(
    ("kind", "level", "group"),
    [
        ("developer", "entry", "exact"),
        ("developer", "mid", "maybe"),
        ("application_support", "unsure", "near"),
        ("data_analysis", "entry", "near"),
        ("industrial_automation", "entry", "maybe"),
        ("industrial_automation", "mid", "maybe"),
        ("industrial_automation", "senior", None),
        ("sales_marketing", "entry", None),
        ("business_tech", "unsure", None),
        ("customer_service", "entry", None),
    ],
)
def test_v3_groups(kind, level, group):
    assert jev_triage.group(record(kind, level, "v3"), "primary", "v3") == group


def test_v3_has_no_industrial_tab_and_industrial_never_matches():
    v3 = jev_triage.V3
    assert all(k in v3.kinds and lv in v3.levels for k, lv in v3.groups)
    assert set(v3.kind_labels) == set(v3.kinds) and set(v3.sort_kinds) <= set(v3.kinds)
    assert not jev_triage.has_industrial_tab("v3") and jev_triage.has_industrial_tab("v2")
    assert "industrial" not in jev_triage.V3.groups.values()
    for version in jev_triage.VERSIONS:
        for kind in jev_triage.INDUSTRIAL_KINDS & set(jev_triage.VERSIONS[version].kinds):
            for level in jev_triage.LEVEL_LABELS:
                found = jev_triage.group(record(kind, level, version), "primary", version)
                assert found not in ("exact", "near")


def test_v3_rules_put_industrial_and_sales_first():
    text = jev_triage.V3.kind_instructions
    assert text.index("industrial_automation") < text.index("sales_marketing")
    assert "even if it also involves\nwriting code" in text
    assert "Salesforce is a software\nplatform" in text
    assert jev_triage.V3.level_instructions == jev_triage.V2.level_instructions


def test_safety_net_demotes_industrial_even_if_a_table_says_exact(monkeypatch):
    groups = dict(jev_triage.V3.groups) | {("industrial_automation", "entry"): "exact"}
    monkeypatch.setitem(jev_triage.VERSIONS, "v3", replace(jev_triage.V3, groups=groups))
    assert jev_triage.group(record("industrial_automation", "entry", "v3"), "primary", "v3") == (
        "maybe"
    )


def test_v4_only_narrows_industrial_wording():
    v3, v4 = jev_triage.V3, jev_triage.V4
    changed = {k for k in v3.kinds if v3.kinds[k] != v4.kinds[k]}
    assert changed == {"industrial_automation", "other"}
    assert (v4.groups, v4.levels, v4.level_instructions) == (
        v3.groups,
        v3.levels,
        v3.level_instructions,
    )
    text = " ".join(v4.kind_instructions.split())
    assert "operates or maintains robots or automated cells" in text
    assert "(CNC, press, grinder, molding or packaging operators)" in text
    assert v4.filename != v3.filename
