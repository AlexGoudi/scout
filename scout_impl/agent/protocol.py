"""The action protocol between the agent loop and the model: a form, constrained both ways.

A 7B model is weak at open-ended tool use and good at filling in a form, so it is given a
form. Every reply is one JSON object whose shape the provider constrains with a schema
(ollama's `format`), and whose shape this module checks again on the way back, because a
constraint the decoder was asked to honour is not one the code may assume.

A reply either requests one tool or answers. How many tool steps a question gets is the
loop's decision, not the model's: on the last permitted step the schema offers `answer`
alone. An answer is a list of items keyed by the group ids the prompt handed out. `group`
and `job_group` are free strings rather than enums on purpose: a grammar that made an
unsolicited group or an invented job group unwritable would also make the checks that
catch them unfalsifiable, and how often they fire is a measurement this stage reports.

Within an item the reason comes before the verdict. The decoder writes properties in
schema order, so the model states why before it states what, rather than justifying an
answer it has already committed to.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..detectors.base import QUESTION_AMBIGUITY, QUESTION_MATERIALITY
from ..provider import RESPONSE_FORMAT, ToolSpec
from ..static.schema import ValidationError, validate

ACTION_ANSWER = "answer"
ACTION_READ = "read_blob"
ACTION_LIST = "list_tree"
ACTION_GREP = "grep"
TOOL_ACTIONS = (ACTION_READ, ACTION_LIST, ACTION_GREP)

VERDICT_MATERIAL = "material"
VERDICT_PASS_THROUGH = "pass-through"
VERDICT_UNCLEAR = "unclear"
VERDICTS = (VERDICT_MATERIAL, VERDICT_PASS_THROUGH, VERDICT_UNCLEAR)

MAX_REASON_CHARS = 400
MAX_CITES = 8

_EVIDENCE_ID = re.compile(r"\bE(\d+)\b", re.IGNORECASE)
_GROUP_PREFIX = re.compile(r"^(group|grp)\s*[:#-]?\s*", re.IGNORECASE)


class MalformedReply(ValueError):
    """The model's reply is not JSON, or not the JSON the schema asked for."""


@dataclass(frozen=True)
class ToolRequest:
    """One read the model asked for before answering."""

    action: str
    args: Dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        shown = ", ".join(f"{key}={value!r}" for key, value in sorted(self.args.items()))
        return f"{self.action}({shown})"


@dataclass(frozen=True)
class AnswerItem:
    """The model's answer for one group, normalized but not yet checked."""

    group: str
    reason: str
    cite: Tuple[str, ...]
    verdict: str = ""
    covered: Optional[bool] = None
    job_group: str = ""

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"group": self.group, "reason": self.reason, "cite": list(self.cite)}
        if self.verdict:
            payload["verdict"] = self.verdict
        if self.covered is not None:
            payload["covered"] = self.covered
            payload["job_group"] = self.job_group
        return payload


@dataclass(frozen=True)
class Reply:
    """One parsed reply: a tool request, or the answers."""

    action: str
    tool: Optional[ToolRequest]
    answers: Tuple[AnswerItem, ...]
    raw: Dict[str, Any]


def answer_item_schema(kind: str) -> Dict[str, Any]:
    if kind == QUESTION_AMBIGUITY:
        properties = {
            "group": {"type": "string"},
            "reason": {"type": "string"},
            "covered": {"type": "boolean"},
            "job_group": {"type": "string"},
            "cite": {"type": "array", "items": {"type": "string"}},
        }
    elif kind == QUESTION_MATERIALITY:
        properties = {
            "group": {"type": "string"},
            "reason": {"type": "string"},
            "verdict": {"enum": list(VERDICTS)},
            "cite": {"type": "array", "items": {"type": "string"}},
        }
    else:
        raise ValueError(f"No answer form for question kind {kind!r}")
    return {"type": "object", "additionalProperties": False, "properties": properties, "required": list(properties)}


def response_schema(kind: str, tools: bool) -> Dict[str, Any]:
    """The whole reply's schema. With `tools` false, answering is the only thing it can say."""
    answers = {"type": "array", "items": answer_item_schema(kind)}
    if not tools:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {"action": {"enum": [ACTION_ANSWER]}, "answers": answers},
            "required": ["action", "answers"],
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {"enum": [ACTION_ANSWER, *TOOL_ACTIONS]},
            "path": {"type": "string"},
            "rev": {"enum": ["head", "base"]},
            "start": {"type": "integer"},
            "end": {"type": "integer"},
            "prefix": {"type": "string"},
            "pattern": {"type": "string"},
            "glob": {"type": "string"},
            "answers": answers,
        },
        "required": ["action"],
    }


def response_format(kind: str, tools: bool) -> ToolSpec:
    return ToolSpec(name=RESPONSE_FORMAT, description="The reply's JSON schema",
                    parameters=response_schema(kind, tools))


def parse_reply(text: str, kind: str, tools: bool) -> Reply:
    """Parse and validate one reply. Raises `MalformedReply` saying what was wrong."""
    payload = _load_json(text)
    try:
        validate(payload, response_schema(kind, tools))
    except ValidationError as error:
        raise MalformedReply(f"reply does not match the schema: {'; '.join(error.problems[:4])}") from None

    action = payload["action"]
    if action in TOOL_ACTIONS:
        args = {key: payload[key] for key in ("path", "rev", "start", "end", "prefix", "pattern", "glob")
                if key in payload}
        return Reply(action=action, tool=ToolRequest(action=action, args=args), answers=(), raw=payload)

    items = tuple(_answer(item, kind) for item in payload.get("answers") or [])
    return Reply(action=ACTION_ANSWER, tool=None, answers=items, raw=payload)


def normalize_group(value: str) -> str:
    return _GROUP_PREFIX.sub("", str(value or "").strip()).strip().upper()


def normalize_job_group(value: str) -> str:
    name = str(value or "").strip().strip("`'\"").lower()
    return name.split(":", 1)[1] if name.startswith("ci_job_group:") else name


def normalize_cites(values: List[Any]) -> Tuple[str, ...]:
    """Evidence ids in the order cited, deduplicated. `"E1, e3"` in one string counts as two."""
    seen: Dict[str, None] = {}
    for value in values:
        text = str(value)
        found = _EVIDENCE_ID.findall(text)
        if not found and text.strip():
            seen.setdefault(text.strip(), None)
        for number in found:
            seen.setdefault(f"E{int(number)}", None)
    return tuple(seen)[:MAX_CITES]


def _answer(item: Dict[str, Any], kind: str) -> AnswerItem:
    reason = " ".join(str(item.get("reason") or "").split())[:MAX_REASON_CHARS]
    common = {"group": normalize_group(item.get("group", "")), "reason": reason,
              "cite": normalize_cites(item.get("cite") or [])}
    if kind == QUESTION_AMBIGUITY:
        return AnswerItem(covered=bool(item.get("covered")), job_group=normalize_job_group(item.get("job_group", "")),
                          **common)
    return AnswerItem(verdict=str(item.get("verdict") or ""), **common)


def _load_json(text: str) -> Dict[str, Any]:
    body = (text or "").strip()
    if body.startswith("```"):
        body = body.strip("`")
        body = body[4:] if body.lower().startswith("json") else body
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise MalformedReply(f"reply is not JSON ({error.msg} at character {error.pos}): {body[:120]!r}") from None
    if not isinstance(payload, dict):
        raise MalformedReply(f"reply is JSON but not an object: {body[:120]!r}")
    return payload
