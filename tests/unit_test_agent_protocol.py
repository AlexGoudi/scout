"""The action protocol: what a reply may say, and how a malformed one is refused."""

import json

import pytest

from scout_impl.agent.protocol import (
    ACTION_ANSWER,
    ACTION_READ,
    TOOL_ACTIONS,
    VERDICTS,
    MalformedReply,
    answer_item_schema,
    normalize_cites,
    normalize_group,
    normalize_job_group,
    parse_reply,
    response_format,
    response_schema,
)
from scout_impl.detectors.base import QUESTION_AMBIGUITY, QUESTION_MATERIALITY
from scout_impl.provider import RESPONSE_FORMAT


def test_the_last_step_schema_can_only_answer():
    schema = response_schema(QUESTION_MATERIALITY, tools=False)
    assert schema["properties"]["action"] == {"enum": [ACTION_ANSWER]}
    assert schema["required"] == ["action", "answers"]
    assert "path" not in schema["properties"]


def test_a_step_with_reads_left_offers_the_three_tools():
    schema = response_schema(QUESTION_AMBIGUITY, tools=True)
    assert schema["properties"]["action"]["enum"] == [ACTION_ANSWER, *TOOL_ACTIONS]
    assert schema["required"] == ["action"]


def test_the_reason_is_written_before_the_verdict():
    """The decoder writes properties in schema order, so the model says why before what."""
    for kind, verdict in ((QUESTION_MATERIALITY, "verdict"), (QUESTION_AMBIGUITY, "covered")):
        order = list(answer_item_schema(kind)["properties"])
        assert order.index("reason") < order.index(verdict)


def test_group_and_job_group_stay_free_strings_so_the_checks_can_fire():
    """An enum would make an unsolicited group or an invented job group unwritable, and uncountable."""
    properties = answer_item_schema(QUESTION_AMBIGUITY)["properties"]
    assert properties["group"] == {"type": "string"}
    assert properties["job_group"] == {"type": "string"}


def test_the_response_format_rides_in_tools_under_the_reserved_name():
    spec = response_format(QUESTION_MATERIALITY, tools=False)
    assert spec.name == RESPONSE_FORMAT
    assert spec.parameters == response_schema(QUESTION_MATERIALITY, tools=False)


def test_an_answer_parses_into_normalized_items():
    text = json.dumps({"action": "answer", "answers": [
        {"group": "group a", "reason": "  adds   a daemon option ", "verdict": "material", "cite": ["e1", "E3, E4"]},
    ]})
    reply = parse_reply(text, QUESTION_MATERIALITY, tools=False)
    assert reply.action == ACTION_ANSWER and reply.tool is None
    (item,) = reply.answers
    assert (item.group, item.reason, item.verdict, item.cite) == ("A", "adds a daemon option", "material",
                                                                  ("E1", "E3", "E4"))


def test_an_ambiguity_answer_carries_coverage_and_the_named_group():
    text = json.dumps({"action": "answer", "answers": [
        {"group": "B", "reason": "arm64", "covered": True, "job_group": "ci_job_group:Marvell-Prestera-ARM64",
         "cite": ["E1"]},
    ]})
    (item,) = parse_reply(text, QUESTION_AMBIGUITY, tools=False).answers
    assert item.covered is True
    assert item.job_group == "marvell-prestera-arm64"


def test_a_tool_request_parses_into_a_request_not_an_answer():
    text = json.dumps({"action": "read_blob", "path": "device/acme/x/platform.json", "start": 1, "end": 20})
    reply = parse_reply(text, QUESTION_MATERIALITY, tools=True)
    assert reply.action == ACTION_READ
    assert reply.tool.args == {"path": "device/acme/x/platform.json", "start": 1, "end": 20}
    assert reply.answers == ()


def test_a_tool_request_on_the_last_step_is_malformed():
    text = json.dumps({"action": "read_blob", "path": "x"})
    with pytest.raises(MalformedReply):
        parse_reply(text, QUESTION_MATERIALITY, tools=False)


@pytest.mark.parametrize("text", ["", "not json", "[1, 2]", '{"action": "answer"}',
                                  '{"action": "answer", "answers": [{"group": "A"}]}',
                                  '{"action": "answer", "answers": [], "extra": 1}'])
def test_anything_but_the_requested_shape_is_malformed(text):
    with pytest.raises(MalformedReply):
        parse_reply(text, QUESTION_MATERIALITY, tools=False)


def test_a_verdict_outside_the_three_is_malformed():
    text = json.dumps({"action": "answer", "answers": [
        {"group": "A", "reason": "r", "verdict": "probably", "cite": []}]})
    with pytest.raises(MalformedReply):
        parse_reply(text, QUESTION_MATERIALITY, tools=False)
    assert "probably" not in VERDICTS


def test_a_fenced_reply_is_unwrapped():
    text = "```json\n" + json.dumps({"action": "answer", "answers": []}) + "\n```"
    assert parse_reply(text, QUESTION_MATERIALITY, tools=False).answers == ()


def test_normalizers():
    assert normalize_group(" Group: c ") == "C"
    assert normalize_job_group("`broadcom`") == "broadcom"
    assert normalize_cites(["E2", "e2", "E10", "the diff"]) == ("E2", "E10", "the diff")
