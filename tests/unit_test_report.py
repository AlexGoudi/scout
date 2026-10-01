"""The report: its structure, its confidence bands, refused when it breaks its own contracts."""

import copy
import json

import pytest

import agent_world as world
from agent_world import ScriptedProvider, ambiguity, answers, materiality
from scout_impl.agent.runner import run_agent
from scout_impl.report import ReportContractError, build_report, render_comment, validate_report
from scout_impl.report.builder import Report
from scout_impl.report.scoring import BAND_HIGH, BAND_LOW, BAND_MEDIUM, band_adjudication, breadth_factor, posts
from scout_impl.static.schema import ValidationError

A_CITES = ("E1", "E3", "E4")
B_CITES = ("E1", "E6", "E7")
C_CITES = ("E2", "E9", "E10")


def _report(replies=(), questions=("q-001", "q-002"), provider=True, **kwargs):
    brief = world.brief(questions)
    agent = run_agent(brief, world.source(), world.change_set(),
                      provider=ScriptedProvider(list(replies)) if provider else None)
    return build_report(brief, agent, run_id="run-1", **kwargs), brief


def _finding(report, question):
    return next(finding for finding in report.findings if finding["question"] == question)


def _confirming(c_covered=True):
    return answers(ambiguity("A", False, cite=A_CITES), ambiguity("B", True, "marvell-prestera-arm64", B_CITES),
                   ambiguity("C", c_covered, "marvell-prestera-armhf" if c_covered else "", C_CITES))


# --- structure ------------------------------------------------------------------------------


def test_the_report_has_hld_4_10s_blocks_and_validates():
    report, _ = _report([_confirming(), answers(materiality("A"))])
    payload = report.payload
    assert payload["schema_version"] == "2.0"
    assert set(payload) == {"schema_version", "run", "findings", "suppressed", "degraded_reason"}
    finding = _finding(report, "q-002")
    assert set(finding) == {"id", "detector", "question", "title", "severity", "confidence", "deterministic",
                            "adjudication", "evidence", "affected", "coverage_gap", "verification", "backtest_ref"}
    assert finding["verification"]["method"] == "none"
    validate_report(payload)


def test_the_deterministic_half_is_proven_and_states_the_briefs_numbers():
    report, _ = _report([_confirming(), answers(materiality("A"))])
    deterministic = _finding(report, "q-002")["deterministic"]
    assert deterministic["band"] == "proven"
    assert (deterministic["affected_count"], deterministic["covered_by_pr_ci"], deterministic["uncovered_count"],
            deterministic["never_built"]) == (6, 1, 2, 3)
    assert "declare only families none of them builds (broadcom-dnx)" in deterministic["statement"]


def test_a_material_judgement_over_few_platforms_is_medium_and_posted():
    report, _ = _report([_confirming(), answers(materiality("A"))])
    finding = _finding(report, "q-002")
    adjudication = finding["adjudication"]
    assert (adjudication["resolution"], adjudication["band"], adjudication["posted"]) == ("material", "medium", True)
    assert adjudication["judgement"] == "model"
    assert finding["title"] == "Change materially affects 2 platform(s) PR CI never builds"
    assert finding["confidence"] == {"band": "medium", "score": adjudication["score"], "basis": "adjudication"}


def test_the_rule_candidate_rides_in_the_deterministic_half_marked_as_a_convention():
    report, _ = _report([_confirming(), answers(materiality("A"))])
    finding = _finding(report, "q-001")
    candidate = finding["deterministic"]["rule_candidate"]
    assert (candidate["covered"], candidate["uncovered"], candidate["derivation"]) == (
        2, 1, "naming-convention-inference")
    assert finding["title"] == ("1 of 3 platform(s) declaring marvell-prestera are never built for their "
                                "CPU architecture")
    assert {item["id"]: item["resolution"] for item in finding["affected"]} == {
        world.PRE_AMD64: "uncovered", world.PRE_ARM64: "covered", world.PRE_ARMHF: "covered"}


def test_a_contest_bands_the_adjudication_no_higher_than_medium_and_shows_both_answers():
    report, _ = _report([_confirming(c_covered=False), answers(materiality("A"))])
    adjudication = _finding(report, "q-001")["adjudication"]
    assert adjudication["status"] == "contested"
    assert adjudication["band"] == BAND_MEDIUM
    (contested,) = [answer for answer in adjudication["answers"] if answer["outcome"] == "contested"]
    assert contested["rule"] == {"resolution": "covered", "job_groups": ["marvell-prestera-armhf"]}
    assert contested["covered"] is False
    assert adjudication["resolution"] == "architecture-aware"


def test_every_evidence_item_carries_its_quote_and_role():
    report, _ = _report([_confirming(), answers(materiality("A"))])
    evidence = _finding(report, "q-002")["evidence"]
    assert {item["role"] for item in evidence} == {"cause", "affected", "contract"}
    assert all(item["quote"] for item in evidence)


def test_the_run_block_counts_what_was_asked_answered_and_spent():
    report, _ = _report([_confirming(), answers(materiality("A"))])
    run = report.payload["run"]
    assert (run["questions_in_brief"], run["questions_asked"], run["questions_answered"], run["model_calls"]) == (
        2, 2, 2, 2)
    assert run["cost"] == {"input_tokens": 1000, "output_tokens": 80, "usd": 0.0}
    assert run["blobs_read"] > 7
    assert run["model"]["prompt_sha"] and run["status"] == "complete"
    assert run["checks"]["cited_job_group"] == 0


# --- degraded and empty runs ----------------------------------------------------------------


def test_a_degraded_report_still_carries_the_deterministic_findings_and_their_evidence():
    report, _ = _report(provider=False)
    assert report.status == "degraded"
    assert "no model provider" in report.payload["degraded_reason"]
    finding = _finding(report, "q-002")
    assert finding["adjudication"]["status"] == "not-run"
    assert finding["adjudication"]["band"] is None
    assert finding["confidence"] == {"band": "proven", "score": 1.0, "basis": "deterministic"}
    assert finding["title"] == "Change reaches 2 platform(s) PR CI never builds"
    assert any(item["role"] == "cause" for item in finding["evidence"])
    assert report.payload["run"]["questions_asked"] == 0


def test_a_brief_with_no_questions_reports_no_findings():
    brief = world.brief(questions=())
    agent = run_agent(brief, world.source(), world.change_set(), provider=world.RefusingProvider())
    report = build_report(brief, agent, run_id="run-1")
    assert report.findings == [] and report.status == "complete"
    assert "No coverage gap" in render_comment(report)


def test_a_judgement_citing_only_the_change_is_incomplete_and_withheld():
    """Materiality needs cause and affected; citing the diff alone is kept in the JSON, not posted."""
    report, _ = _report([answers(materiality("A", cite=("E1",)))], questions=("q-002",))
    adjudication = _finding(report, "q-002")["adjudication"]
    assert adjudication["band"] == BAND_LOW and adjudication["posted"] is False
    assert report.payload["suppressed"] == [{"finding": "f-001", "part": "adjudication", "band": "low",
                                             "reason": adjudication["band_reason"]}]
    assert _finding(report, "q-002")["confidence"]["basis"] == "deterministic"


def test_an_unclear_verdict_is_withheld():
    report, _ = _report([answers(materiality("A", verdict="unclear"))], questions=("q-002",))
    assert _finding(report, "q-002")["adjudication"]["band"] == BAND_LOW


def test_a_pass_through_judgement_lowers_severity_and_says_so():
    report, _ = _report([answers(materiality("A", verdict="pass-through", reason="only a comment changed"))],
                        questions=("q-002",))
    finding = _finding(report, "q-002")
    assert finding["severity"] == "low"
    assert finding["title"].startswith("Change passes through 2 platform(s)")


# --- canonical form -------------------------------------------------------------------------


def test_the_canonical_form_drops_only_what_a_rerun_cannot_reproduce():
    first, _ = _report([_confirming(), answers(materiality("A"))])
    second, _ = _report([_confirming(), answers(materiality("A"))])
    assert first.canonical_json() == second.canonical_json()
    canonical = json.loads(first.canonical_json())["run"]
    assert "id" not in canonical and "duration_s" not in canonical and "replayed" not in canonical["model"]
    assert canonical["brief_sha"] == first.payload["run"]["brief_sha"]


# --- contracts the schema cannot state ------------------------------------------------------


def test_a_report_naming_an_entity_outside_the_brief_is_refused():
    report, brief = _report([_confirming(), answers(materiality("A"))])
    payload = copy.deepcopy(report.payload)
    payload["findings"][0]["affected"].append(
        {"kind": "platform", "id": "platform:acme/x86_64-invented-r0", "asic_families": [], "reason": "made up"})
    with pytest.raises(ReportContractError, match="outside the brief"):
        Report(payload).validate(brief)


def test_a_contested_adjudication_banded_high_is_refused():
    report, brief = _report([_confirming(c_covered=False), answers(materiality("A"))])
    payload = copy.deepcopy(report.payload)
    adjudication = next(item for item in payload["findings"] if item["question"] == "q-001")["adjudication"]
    adjudication["band"] = "high"
    with pytest.raises(ReportContractError, match="above medium"):
        Report(payload).validate(brief)


def test_a_finding_for_a_question_the_brief_did_not_ask_is_refused():
    report, brief = _report([_confirming(), answers(materiality("A"))])
    payload = copy.deepcopy(report.payload)
    payload["findings"][0]["question"] = "q-009"
    payload["findings"][0]["adjudication"]["question"] = "q-009"
    with pytest.raises(ReportContractError, match="does not ask"):
        Report(payload).validate(brief)


def test_the_schema_rejects_an_unknown_band():
    report, _ = _report([_confirming(), answers(materiality("A"))])
    payload = copy.deepcopy(report.payload)
    payload["findings"][0]["confidence"]["band"] = "certain"
    with pytest.raises(ValidationError):
        validate_report(payload)


# --- scoring --------------------------------------------------------------------------------


def test_bands_follow_hld_4_9():
    complete = [(("cause", "affected"), 3)]
    assert band_adjudication(0.7, ["cause", "affected"], complete, False, False).band == BAND_HIGH
    assert band_adjudication(0.7, ["cause", "affected"], [(("cause", "affected"), 1)], False, False).band == BAND_MEDIUM
    assert band_adjudication(0.7, ["cause", "affected"], complete, True, False).band == BAND_MEDIUM
    assert band_adjudication(0.7, ["cause", "affected"], [(("cause",), 3)], False, False).band == BAND_LOW
    assert band_adjudication(0.4, ["cause"], [(("cause",), 3)], False, False).band == BAND_LOW
    assert band_adjudication(0.7, ["cause"], [], False, False).band is None


def test_breadth_saturates_and_medium_posts_only_while_few_rank_higher():
    assert breadth_factor(0) == 0.5
    assert breadth_factor(16) == pytest.approx(1.0) == breadth_factor(400)
    assert posts("high", 9) and posts("medium", 4) and not posts("medium", 5) and not posts("low", 0)
