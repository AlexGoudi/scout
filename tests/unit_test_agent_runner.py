"""The agent loop end to end over the world's brief, with a scripted model.

Each test scripts the model's replies and asserts what the code made of them, because the
point of the stage is that the answer a report carries is decided by the checks, not by the
model: a right answer for a wrong reason is dropped, a disagreement with the rule is kept
and marked, and no failure of the model or of a budget ever raises out of `run_agent`.
"""

import json

import agent_world as world
from agent_world import ScriptedProvider, ambiguity, answers, materiality
from scout_impl.agent.checks import (
    CHECK_CITATION,
    CHECK_CITED_JOB_GROUP,
    CHECK_ENTITY_CLOSURE,
    CHECK_QUESTION_BINDING,
    CHECK_RULE_CONSISTENCY,
)
from scout_impl.agent.prompts import PROMPT_SHA, SYSTEM_PROMPT
from scout_impl.agent.runner import MAX_TOOL_STEPS, run_agent
from scout_impl.core.runlog import RunLog
from scout_impl.provider import ProviderError

# The ambiguity question's evidence, in the order assembly numbers it: the brief's two
# quoted job groups, then per group (amd64 A, arm64 B, armhf C) its declaration and both
# sides of its change.
A_CITES = ("E1", "E3", "E4")
B_CITES = ("E1", "E6", "E7")
C_CITES = ("E2", "E9", "E10")


def _run(replies, questions=("q-001", "q-002"), **kwargs):
    provider = ScriptedProvider(replies)
    log = RunLog()
    brief_kwargs = {key: kwargs.pop(key) for key in ("budget", "question_budget", "mode") if key in kwargs}
    change_set = kwargs.pop("change_set", world.change_set())
    result = run_agent(world.brief(questions, **brief_kwargs), world.source(), change_set=change_set,
                       provider=provider, run_log=log, **kwargs)
    return result, provider, log


def _confirming():
    return answers(ambiguity("A", False, cite=A_CITES), ambiguity("B", True, "marvell-prestera-arm64", B_CITES),
                   ambiguity("C", True, "marvell-prestera-armhf", C_CITES))


def _items(result, question):
    return {item.group: item for item in result.question(question).items}


# --- the happy path, and what it costs ------------------------------------------------------


def test_both_questions_are_answered_and_every_check_stays_quiet():
    result, provider, log = _run([_confirming(), answers(materiality("A"))])

    assert result.status == "complete"
    assert [question.status for question in result.questions] == ["answered", "answered"]
    assert {item.outcome for item in result.question("q-001").items} == {"confirmed"}
    assert _items(result, "q-002")["A"].outcome == "answered"
    assert set(result.checks.values()) == {0}
    assert result.model_calls == 2 == len(provider.requests)
    assert (result.usage.input_tokens, result.usage.output_tokens) == (1000, 80)
    assert result.prompt_sha == PROMPT_SHA
    assert log.count("model_call") == 2


def test_a_run_counts_its_own_tokens_when_a_provider_is_reused():
    """A backtest reuses one provider across briefs; each run reports only what it spent."""
    provider = ScriptedProvider([answers(materiality("A")), answers(materiality("A"))])
    first = run_agent(world.brief(("q-002",)), world.source(), world.change_set(), provider=provider)
    second = run_agent(world.brief(("q-002",)), world.source(), world.change_set(), provider=provider)
    assert first.usage == second.usage and second.usage.input_tokens == 500
    assert provider.usage.input_tokens == 1000


def test_the_rule_answer_is_in_front_of_the_model_for_it_to_confirm_or_contest():
    _, provider, _ = _run([_confirming()], questions=("q-001",))
    system, user = provider.requests[0]["messages"]
    assert system == {"role": "system", "content": SYSTEM_PROMPT}
    prompt = user["content"]
    assert "Rule answer: not covered: no job group builds marvell-prestera for amd64" in prompt
    assert "Rule answer: covered by marvell-prestera-arm64" in prompt
    assert "marvell-prestera-armhf: marvell-prestera, armhf" in prompt
    assert "E1 [contract] azure-pipelines.yml:8-11" in prompt


def test_a_shared_file_reached_through_symlinks_is_one_group_with_the_link_as_evidence():
    result, provider, _ = _run([answers(materiality("A"))], questions=("q-002",))
    (group,) = result.question("q-002").assembly.groups
    assert group.members == (world.DNX, world.DNX2)
    assert group.families == ("broadcom-dnx",)
    assert group.reach[0].link == "device/acme/x86_64-acme_dnx-r0/pmon_daemon_control.json"
    prompt = provider.requests[0]["messages"][1]["content"]
    assert f"Reached by: {world.PMON} through a symlink" in prompt
    assert f"E3 [affected] device/acme/x86_64-acme_dnx-r0/pmon_daemon_control.json (symlink to {world.LINK_TARGET})" \
        in prompt


def test_the_change_is_shown_as_a_diff_so_a_trailing_comma_reads_as_one():
    """Found live on #24811: shown as two separate sides, a line that only gained a comma was read as changed."""
    _, provider, _ = _run([answers(materiality("A"))], questions=("q-002",))
    prompt = provider.requests[0]["messages"][1]["content"]
    block = prompt.split("CHANGES (lines starting - were removed, + were added)\n", 1)[1].split("\n\nEVIDENCE")[0]
    assert block.splitlines()[:4] == [
        f"{world.PMON} (modified, +4 -1; cite E1 for lines added, E2 for lines removed):",
        "     {",
        '    -    "skip_fancontrol": true',
        '    +    "skip_fancontrol": true,',
    ]
    assert f"E2 [cause] {world.PMON}:2 at base (modified, +4 -1, lines removed); its lines are under CHANGES" \
        in prompt


def test_one_shared_file_reaching_two_families_is_two_judgements():
    """Found live on #24811: barefoot and broadcom-dnx platforms linking one file were one group,
    and the group spoke for all of them with its representative's family."""
    brief = world.brief(("q-002",))
    brief["questions"][0]["entities"] = [world.BCM, world.DNX, world.DNX2]
    result = run_agent(brief, world.source(), world.change_set(),
                       provider=ScriptedProvider([answers(materiality("A"), materiality("B"))]))
    groups = result.question("q-002").assembly.groups
    assert [(group.members, group.families) for group in groups] == [
        ((world.BCM,), ("broadcom",)), ((world.DNX, world.DNX2), ("broadcom-dnx",))]


def test_the_example_answer_cites_the_first_groups_own_ids_and_offers_every_verdict():
    """A 7B model copies the example's ids, so the example must not show arbitrary ones."""
    _, provider, _ = _run([answers(materiality("A"))], questions=("q-002",))
    prompt = provider.requests[0]["messages"][1]["content"]
    assert '"verdict": "material" or "pass-through" or "unclear", "cite": ["E1", "E3"]' in prompt
    assert "Its evidence: cause E1, E2; affected E3, E4." in prompt


# --- each check, firing ---------------------------------------------------------------------


def test_a_right_answer_for_a_wrong_reason_is_dropped_by_the_cited_job_group_check():
    """HLD 4.5.1 case C: right that it is covered, wrong about which job group builds it."""
    reply = answers(ambiguity("A", False, cite=A_CITES), ambiguity("B", True, "marvell-prestera-armhf", B_CITES),
                    ambiguity("C", True, "marvell-prestera-armhf", C_CITES))
    result, _, _ = _run([reply], questions=("q-001",))
    item = _items(result, "q-001")["B"]
    assert (item.outcome, item.check, item.agrees) == ("dropped", CHECK_CITED_JOB_GROUP, True)
    assert result.checks[CHECK_CITED_JOB_GROUP] == 1


def test_disagreeing_with_the_rule_is_kept_contested_and_never_overrides_it():
    """HLD 4.5.1 case D: the armhf platform called not covered, with the armhf group in the list."""
    reply = answers(ambiguity("A", False, cite=A_CITES), ambiguity("B", True, "marvell-prestera-arm64", B_CITES),
                    ambiguity("C", False, "", C_CITES, reason="no job group builds armhf"))
    result, _, log = _run([reply], questions=("q-001",))
    item = _items(result, "q-001")["C"]
    assert (item.outcome, item.check, item.agrees) == ("contested", CHECK_RULE_CONSISTENCY, False)
    assert "the rule says covered by marvell-prestera-armhf" in item.detail
    assert item.counter_evidence is False
    assert result.checks[CHECK_RULE_CONSISTENCY] == 1
    assert log.count("check", check=CHECK_RULE_CONSISTENCY) == 1


def test_a_contest_citing_something_beyond_the_rules_own_premises_counts_as_counter_evidence():
    reply = answers(ambiguity("A", False, cite=A_CITES), ambiguity("B", True, "marvell-prestera-arm64", B_CITES),
                    ambiguity("C", False, "", ("E2", "E9", "E10", "E3")))
    result, _, _ = _run([reply], questions=("q-001",))
    assert _items(result, "q-001")["C"].counter_evidence is True


def test_an_invented_job_group_is_dropped_by_entity_closure_before_anything_else():
    reply = answers(ambiguity("A", True, "marvell-prestera-amd64", A_CITES),
                    ambiguity("B", True, "marvell-prestera-arm64", B_CITES),
                    ambiguity("C", True, "marvell-prestera-armhf", C_CITES))
    result, _, _ = _run([reply], questions=("q-001",))
    item = _items(result, "q-001")["A"]
    assert (item.outcome, item.check) == ("dropped", CHECK_ENTITY_CLOSURE)
    assert result.checks[CHECK_CITED_JOB_GROUP] == 0


def test_an_invented_platform_in_a_materiality_reason_is_dropped_by_entity_closure():
    reply = answers(materiality("A", reason="like x86_64-acme_other-r0, these load the file"))
    result, _, _ = _run([reply], questions=("q-002",))
    assert _items(result, "q-002")["A"].check == CHECK_ENTITY_CLOSURE
    assert result.question("q-002").status == "unanswered"
    assert result.status == "partial"


def test_an_answer_to_a_group_nobody_asked_about_is_dropped_and_counted():
    reply = answers(materiality("A"), materiality("Z"), materiality("A"))
    result, _, log = _run([reply], questions=("q-002",))
    outcomes = [(item.group, item.outcome, item.check) for item in result.question("q-002").items]
    assert outcomes == [("A", "answered", ""), ("Z", "dropped", CHECK_QUESTION_BINDING),
                        ("A", "dropped", CHECK_QUESTION_BINDING)]
    assert result.checks[CHECK_QUESTION_BINDING] == 2
    assert log.count("check", check=CHECK_QUESTION_BINDING) == 2


def test_citing_evidence_that_was_never_shown_is_dropped_by_the_resolver():
    result, _, _ = _run([answers(materiality("A", cite=("E1", "E42")))], questions=("q-002",))
    item = _items(result, "q-002")["A"]
    assert (item.outcome, item.check) == ("dropped", CHECK_CITATION)
    assert "E42" in item.detail


def test_a_change_set_that_disagrees_with_the_tree_is_caught_on_re_read():
    """The diff says line 3 reads one way and the head tree another: the finding must not stand."""
    tampered = world.DIFF.replace('+        "poll_interval": 10', '+        "poll_interval": 99')
    result, _, _ = _run([answers(materiality("A"))], questions=("q-002",), change_set=world.change_set(tampered))
    item = _items(result, "q-002")["A"]
    assert (item.outcome, item.check) == ("dropped", CHECK_CITATION)


def test_a_group_left_unanswered_is_recorded_as_such():
    reply = answers(ambiguity("A", False, cite=A_CITES))
    result, _, _ = _run([reply], questions=("q-001",))
    assert {group: item.outcome for group, item in _items(result, "q-001").items()} == {
        "A": "confirmed", "B": "unanswered", "C": "unanswered"}
    assert result.question("q-001").status == "answered"


# --- the bounded tool protocol --------------------------------------------------------------


def test_a_read_becomes_numbered_evidence_the_answer_can_cite():
    read = {"action": "read_blob", "path": "device/acme/x86_64-acme_dnx-r0/README.md"}
    result, provider, log = _run([read, answers(materiality("A", cite=("E1", "E12")))], questions=("q-002",))
    question = result.question("q-002")
    assert question.tool_steps == 1 and len(question.calls) == 2
    assert _items(result, "q-002")["A"].outcome == "answered"
    observation = provider.requests[1]["messages"][-1]["content"]
    assert observation.startswith("RESULT of read_blob(path='device/acme/x86_64-acme_dnx-r0/README.md'):")
    assert "E12 [affected] device/acme/x86_64-acme_dnx-r0/README.md:1" in observation
    assert log.count("tool_call", ok=True) == 1


def test_after_the_last_permitted_read_the_schema_can_only_answer():
    reads = [{"action": "list_tree", "prefix": "device/acme/x86_64-acme_dnx-r0"}] * MAX_TOOL_STEPS
    result, provider, _ = _run(reads + [answers(materiality("A"))], questions=("q-002",))
    schemas = [request["schema"]["properties"]["action"]["enum"] for request in provider.requests]
    assert [len(actions) for actions in schemas] == [4] * MAX_TOOL_STEPS + [1]
    assert "No more reads are allowed. Answer now." in provider.requests[-1]["messages"][-1]["content"]
    assert result.question("q-002").status == "answered"


def test_a_failed_read_is_an_observation_not_a_crash():
    read = {"action": "read_blob", "path": "device/acme/nowhere.txt"}
    result, provider, log = _run([read, answers(materiality("A"))], questions=("q-002",))
    assert "error: device/acme/nowhere.txt is not a file" in provider.requests[1]["messages"][-1]["content"]
    assert log.count("tool_call", ok=False) == 1
    assert result.question("q-002").status == "answered"


def test_no_reads_are_offered_when_the_question_cannot_afford_one():
    result, provider, _ = _run([answers(materiality("A"))], questions=("q-002",),
                               question_budget={"tool_calls": 0, "blob_reads": 6})
    assert provider.requests[0]["schema"]["properties"]["action"]["enum"] == ["answer"]
    assert "No more reads are allowed" in provider.requests[0]["messages"][1]["content"]


# --- budgets and degraded runs --------------------------------------------------------------


def test_a_brief_with_no_questions_costs_no_model_call_at_all():
    provider = world.RefusingProvider()
    result = run_agent(world.brief(questions=()), world.source(), world.change_set(), provider=provider)
    assert result.status == "complete"
    assert result.model_calls == 0 and result.questions == []


def test_no_provider_degrades_but_still_gathers_the_evidence():
    result = run_agent(world.brief(), world.source(), world.change_set(), provider=None)
    assert result.status == "degraded"
    assert "no model provider" in result.degraded_reason
    assert [question.status for question in result.questions] == ["not-run", "not-run"]
    assert result.question("q-002").assembly.groups[0].families == ("broadcom-dnx",)


def test_an_unreachable_model_degrades_after_one_preflight_and_no_question():
    provider = ScriptedProvider(preflight_error="Nothing answered at http://127.0.0.1:1")
    result = run_agent(world.brief(), world.source(), world.change_set(), provider=provider)
    assert result.status == "degraded"
    assert "Nothing answered" in result.degraded_reason
    assert provider.preflights == 1 and provider.requests == []


def test_a_provider_failing_mid_run_keeps_what_was_answered_and_degrades():
    result, _, _ = _run([_confirming(), ProviderError("llama runner process has terminated")])
    assert result.status == "degraded"
    assert result.question("q-001").status == "answered"
    assert result.question("q-002").status == "unanswered"


def test_an_exhausted_question_budget_truncates_that_question_before_any_call():
    """The link and the declaration are two distinct blobs; a budget of one cannot assemble both."""
    result, provider, log = _run([], questions=("q-002",), question_budget={"tool_calls": 12, "blob_reads": 1})
    assert result.question("q-002").status == "truncated"
    assert "blob_reads" in result.question("q-002").reason
    assert provider.requests == []
    assert result.status == "partial"
    assert result.truncated == ["q-002"]
    assert log.count("budget_exhausted", scope="question") == 1


def test_the_run_deadline_stops_the_rest_and_degrades():
    ticks = iter(range(0, 10_000, 100))
    result, provider, _ = _run([_confirming()], clock=lambda: float(next(ticks)), deadline_s=50)
    assert result.status == "degraded"
    assert "wall_clock" in result.degraded_reason
    assert provider.requests == []
    assert [question.status for question in result.questions] == ["truncated", "not-run"]


def test_an_unusable_reply_leaves_the_question_unanswered():
    result, _, log = _run(["this is not json"], questions=("q-002",))
    assert result.question("q-002").status == "unanswered"
    assert "unusable" in result.question("q-002").reason
    assert result.status == "partial"
    assert log.events("model_call")[0]["action"] == "malformed"


def test_a_whole_tree_brief_skips_materiality_without_asking():
    result, provider, _ = _run([], questions=("q-002",), mode="tree", change_set=None)
    assert result.question("q-002").status == "skipped"
    assert provider.requests == []
    assert result.status == "complete"


def test_every_excerpt_sent_to_the_model_is_named_in_the_run_log():
    """NFR-9: what left the trust boundary is auditable, by path, revision and line range."""
    _, _, log = _run([answers(materiality("A"))], questions=("q-002",))
    (call,) = log.events("model_call")
    sent = {(item["path"], item["rev"]) for item in call["excerpts"]}
    assert (world.PMON, "head") in sent and (world.PMON, "base") in sent
    assert json.loads(call["reply"])["answers"][0]["group"] == "A"


def test_the_consulted_paths_are_reported_for_fixture_capture():
    result, _, _ = _run([_confirming(), answers(materiality("A", cite=("E1", "E2", "E3")))])
    assert "device/acme/x86_64-acme_dnx-r0/pmon_daemon_control.json" in result.consulted["head"]
    assert world.PMON in result.consulted["base"]
