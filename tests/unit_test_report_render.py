"""The advisory comment: fact and judgement kept visibly apart, and capped."""

import copy

import agent_world as world
from agent_world import ScriptedProvider, ambiguity, answers, materiality
from scout_impl.agent.runner import run_agent
from scout_impl.report import build_report, render_comment
from scout_impl.report.render import MAX_CHARS, MAX_NAMED

A_CITES = ("E1", "E3", "E4")
B_CITES = ("E1", "E6", "E7")
C_CITES = ("E2", "E9", "E10")


def _comment(replies=(), questions=("q-001", "q-002"), provider=True):
    brief = world.brief(questions)
    agent = run_agent(brief, world.source(), world.change_set(),
                      provider=ScriptedProvider(list(replies)) if provider else None)
    report = build_report(brief, agent, run_id="run-1")
    return render_comment(report), report


def _replies(c_covered=True, b_group="marvell-prestera-arm64"):
    return [answers(ambiguity("A", False, cite=A_CITES), ambiguity("B", True, b_group, B_CITES),
                    ambiguity("C", c_covered, "marvell-prestera-armhf" if c_covered else "", C_CITES,
                              reason="no job group builds armhf")),
            answers(materiality("A"))]


def test_it_says_it_is_advisory_and_what_a_fact_and_a_judgement_are():
    text, _ = _comment(_replies())
    assert "Advisory only: Scout never blocks a merge." in text
    assert "**Facts** are computed from the tree" in text and "**Judgements** are a local model's" in text


def test_each_finding_renders_the_fact_its_snippet_and_the_platforms_before_the_judgement():
    text, _ = _comment(_replies())
    section = text.split("### ")[2]
    fact = section.index("**Fact** (proven from the tree")
    snippet = section.index("```text")
    platforms = section.index("**Never built by PR CI:** 2 platform(s), declaring `broadcom-dnx`")
    judgement = section.index("**Judgement** (`scripted-model-2026-09-25` output, not a fact")
    assert fact < snippet < platforms < judgement
    assert '    "xcvrd": {' in section


def test_the_rule_and_the_model_are_shown_side_by_side_with_what_the_checks_did():
    text, _ = _comment(_replies(c_covered=False))
    assert "| Group | Platforms | Rule answer | Model answer | Outcome |" in text
    assert "contested without counter-evidence; the rule's answer stands" in text
    assert "covered by `marvell-prestera-armhf`" in text
    assert '"no job group builds armhf"' in text


def test_a_dropped_claim_names_the_check_that_dropped_it():
    text, _ = _comment(_replies(b_group="marvell-prestera-armhf"))
    assert "dropped by the cited job group check" in text


def test_a_degraded_comment_says_so_and_still_states_the_facts():
    text, _ = _comment(provider=False)
    assert "> **Degraded:** no model provider was configured" in text
    assert text.count("**Fact** (proven from the tree") == 2
    assert "none. No model provider was configured" in text


def test_a_withheld_judgement_is_noted_not_shown():
    text, _ = _comment([answers(materiality("A", cite=("E1",), reason="secret reasoning"))], questions=("q-002",))
    assert "withheld, confidence low" in text
    assert "secret reasoning" not in text


def _materiality_payload():
    _, report = _comment(_replies())
    payload = copy.deepcopy(report.payload)
    return payload, next(item for item in payload["findings"] if item["question"] == "q-002")


def test_platform_names_are_capped():
    payload, finding = _materiality_payload()
    finding["affected"] = [dict(finding["affected"][0], id=f"platform:acme/x86_64-acme_{index}-r0")
                           for index in range(40)]
    text = render_comment(payload)
    assert f"`acme/x86_64-acme_{MAX_NAMED - 1}-r0`, and {40 - MAX_NAMED} more." in text
    assert f"acme/x86_64-acme_{MAX_NAMED}-r0" not in text


def test_the_comment_is_capped_in_length():
    payload, finding = _materiality_payload()
    finding["deterministic"]["statement"] = "x" * (MAX_CHARS * 2)
    text = render_comment(payload)
    assert len(text) <= MAX_CHARS + 200
    assert text.rstrip().endswith("the whole report is `scout-report.json`._")


def test_the_footer_counts_calls_tokens_and_check_firings():
    text, _ = _comment(_replies(c_covered=False))
    assert "2 model call(s), 1000 tokens in, 80 out" in text
    assert "rule consistency 1" in text
