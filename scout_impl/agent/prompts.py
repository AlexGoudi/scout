"""The pinned prompts, and the rendering that fills them from an assembly (NFR-3).

The system prompt is one constant for every question, so its hash, `PROMPT_SHA`, names the
prompt a report was produced under; it also covers the task templates and the answer
schemas, because a change to either changes what the model is asked. Everything a prompt
says is derived from the brief and the tree in a fixed order, with no run id, clock or
local path in it, which is what lets a replay key match the recording it was made from.

Two things the prompts deliberately do not do. They do not ask the model to decide the
architecture question: the rule's answer is printed beside each group and the model is
asked to confirm it or to contest it with evidence, because at 7B it was measured flipping
that answer with the wording and inventing reasons for right ones (HLD section 4.5.1). And
they do not rely on the model to obey its rules: citing only listed ids, naming only listed
entities and answering only listed groups are all checked in code afterwards, and the
prompt states them only so that a compliant answer is the likely one.
"""

import hashlib
import json
from typing import Dict, List, Sequence

from ..detectors.base import QUESTION_AMBIGUITY, QUESTION_MATERIALITY
from ..models import FileDiff
from .assemble import Assembly, BriefView, Group, members_named, required_roles
from .evidence import Evidence
from .protocol import ACTION_ANSWER, answer_item_schema
from .toolbox import REV_BASE

# Bump on any change to how a prompt is rendered: `PROMPT_SHA` hashes the templates below,
# not the code that fills them.
PROMPT_VERSION = "d6-2026-09-25.4"
MAX_NAMED = 6
MAX_QUOTE_LINES = 8
MAX_DIFF_LINES = 16

SYSTEM_PROMPT = (
    "You are the adjudicator of SONiC Scout, an advisory reviewer of sonic-buildimage changes. "
    "You answer one question about the groups you are given, using only the numbered evidence you are shown.\n"
    "- Cite evidence only by its id, such as E2. An answer citing no valid id is discarded.\n"
    "- Name only platforms and job groups that appear in the question or the evidence.\n"
    "- If the evidence cannot decide, say so rather than guess.\n"
    "- Reply with one JSON object in the requested shape. Keep every reason under 30 words."
)

MATERIALITY_TASK = (
    "No pull-request CI job builds these platforms, so no CI build would notice if this change broke them. "
    "For each group, decide whether the change materially affects the platforms or only passes through "
    "their files.\n"
    "- \"material\": it changes something these platforms build, install, load or run: configuration, "
    "code or data they use, including a file they link to.\n"
    "- \"pass-through\": it touches their files without changing what they do: comments, documentation, "
    "whitespace, or a rename with identical content.\n"
    "- \"unclear\": the evidence cannot tell.\n"
    "In the reason, say what the change alters for these platforms, not only which file it touches."
)

AMBIGUITY_TASK = (
    "The static rule has already answered for each group below, by applying the rules above. Confirm its "
    "answer, or contest it only if the evidence shows the rule does not hold for that group. For each group "
    "say whether its platforms are covered and, if covered, name the one job group that builds a family "
    "they declare for their CPU architecture."
)

TOOLS_TEXT = (
    "Before answering you may read the tree {steps} more time(s), with one of:\n"
    "{{\"action\": \"read_blob\", \"path\": \"<file>\", \"rev\": \"head\", \"start\": 1, \"end\": 40}}\n"
    "{{\"action\": \"list_tree\", \"prefix\": \"<directory>\"}}\n"
    "{{\"action\": \"grep\", \"pattern\": \"<regex>\", \"glob\": \"<glob matching at most 6 files>\"}}\n"
    "Read only if the evidence above cannot decide."
)

LAST_STEP_TEXT = "No more reads are allowed. Answer now."


def _sha() -> str:
    material = {
        "version": PROMPT_VERSION,
        "system": SYSTEM_PROMPT,
        "tasks": [MATERIALITY_TASK, AMBIGUITY_TASK, TOOLS_TEXT, LAST_STEP_TEXT],
        "schemas": [answer_item_schema(QUESTION_MATERIALITY), answer_item_schema(QUESTION_AMBIGUITY)],
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


PROMPT_SHA = _sha()


def render_question(assembly: Assembly, brief: BriefView, tool_steps: int) -> str:
    """The first user turn for one question."""
    if assembly.kind == QUESTION_AMBIGUITY:
        body = _ambiguity(assembly, brief)
    else:
        body = _materiality(assembly)
    closing = [_answer_line(assembly), _cite_line(assembly)]
    closing.append(TOOLS_TEXT.format(steps=tool_steps) if tool_steps > 0 else LAST_STEP_TEXT)
    return "\n\n".join([body, "\n".join(closing)])


def render_observation(request: str, evidence: Sequence[Evidence], error: str, tool_steps: int) -> str:
    """The user turn answering a tool request: what it returned, numbered for citation."""
    if error:
        lines = [f"RESULT of {request}: error: {error}"]
    else:
        lines = [f"RESULT of {request}:"] + [render_evidence(item) for item in evidence]
        if not evidence:
            lines.append("(nothing)")
    lines.append(TOOLS_TEXT.format(steps=tool_steps) if tool_steps > 0 else LAST_STEP_TEXT)
    return "\n".join(lines)


def render_evidence(item: Evidence, summary: bool = False) -> str:
    where = f"{item.path}:{item.span}"
    if item.origin == "link":
        where = item.path
    at = " at base" if item.rev == REV_BASE else ""
    label = f" ({item.label})" if item.label else ""
    if summary:
        return f"{item.id} [{item.role}] {where}{at}{label}; its lines are under CHANGES"
    rows = item.quote.splitlines() or ["(empty)"]
    if len(rows) > MAX_QUOTE_LINES:
        rows = rows[:MAX_QUOTE_LINES] + [f"... {len(rows) - MAX_QUOTE_LINES} more line(s)"]
    return "\n".join([f"{item.id} [{item.role}] {where}{at}{label}"] + [f"    {row}" for row in rows])


def _materiality(assembly: Assembly) -> str:
    """Groups, then each changed file as a diff, then the evidence ids.

    The change is shown as a diff rather than as its two sides in separate items, because
    judged from separate items a line that only gained a trailing comma was read as changed.
    The diff's own evidence items are then listed by header alone, since their lines are above.
    """
    question = assembly.question
    lines = [f"QUESTION {question['id']} (rule {question['rule']}): {question['ask']}", "", MATERIALITY_TASK, "",
             "GROUPS"]
    for group in assembly.groups:
        lines.append(_materiality_group(group, assembly))
    if assembly.changes:
        lines.extend(["", "CHANGES (lines starting - were removed, + were added)"])
        for file_diff in assembly.changes.values():
            lines.extend(_diff_block(file_diff, assembly))
    lines.extend(["", "EVIDENCE"])
    lines.extend(render_evidence(item, summary=item.origin == "diff" and item.path in assembly.changes)
                 for item in assembly.book.items)
    return "\n".join(lines)


def _diff_block(file_diff: FileDiff, assembly: Assembly) -> List[str]:
    sides = [item for item in assembly.book.items
             if item.origin == "diff" and item.path in (file_diff.path, file_diff.old_path)]
    added = [item.id for item in sides if item.rev != REV_BASE]
    removed = [item.id for item in sides if item.rev == REV_BASE]
    cites = [text for text in (f"cite {', '.join(added)} for lines added" if added else "",
                               f"{', '.join(removed)} for lines removed" if removed else "") if text]
    header = (f"{file_diff.path} ({file_diff.change_type}, +{file_diff.additions} -{file_diff.deletions}"
              f"{'; ' + ', '.join(cites) if cites else ''}):")
    if file_diff.is_binary:
        return [header, "    (binary)"]
    rows: List[str] = []
    for index, hunk in enumerate(file_diff.hunks):
        if index:
            rows.append("    ...")
        rows.extend(f"    {line.kind}{line.content}" for line in hunk.lines)
    if len(rows) > MAX_DIFF_LINES:
        rows = rows[:MAX_DIFF_LINES] + [f"    ... {len(rows) - MAX_DIFF_LINES} more line(s)"]
    return [header] + rows


def _materiality_group(group: Group, assembly: Assembly) -> str:
    names, more = members_named(group.members, MAX_NAMED)
    shown = ", ".join(names) + (f" and {more} more" if more else "")
    families = ", ".join(group.families) or "an unread family"
    paths = []
    for reach in group.reach:
        how = " through a symlink" if reach.link else (" (per the static stage)" if reach.via_brief else "")
        paths.append(f"{reach.path}{how}")
    reached = "; ".join(paths) or "no changed file found reaching them"
    return (f"{group.id}: {len(group.members)} platform(s) declaring {families}: {shown}. "
            f"Reached by: {reached}. Its evidence: {_by_role(group.evidence, assembly)}.")


def _by_role(ids: Sequence[str], assembly: Assembly) -> str:
    """A group's evidence ids named by role, which is what an answer has to cite."""
    roles: Dict[str, List[str]] = {}
    for evidence_id in ids:
        item = assembly.book.get(evidence_id)
        if item is not None:
            roles.setdefault(item.role, []).append(evidence_id)
    return "; ".join(f"{role} {', '.join(found)}" for role, found in roles.items()) or "none"


def _ambiguity(assembly: Assembly, brief: BriefView) -> str:
    question = assembly.question
    candidate = brief.rule_candidate(question) or {}
    rule_ids = list(candidate.get("rules") or [question["rule"]])
    lines = [f"QUESTION {question['id']} (rules {', '.join(rule_ids)}): {question['ask']}", "", "RULES"]
    for rule_id in rule_ids:
        rule = brief.rules.get(rule_id)
        if rule is not None:
            lines.append(f"{rule_id}: {rule['statement']}")
    lines.extend(["", AMBIGUITY_TASK, "", "PR-CI JOB GROUPS (name: family, architecture)"])
    lines.append("; ".join(f"{group['name']}: {group['family']}, {group['arch']}" for group in brief.job_groups))
    lines.extend(["", "GROUPS"])
    for group in assembly.groups:
        lines.append(_ambiguity_group(group, assembly))
    lines.extend(["", "EVIDENCE"])
    lines.extend(render_evidence(item) for item in assembly.book.items)
    return "\n".join(lines)


def _ambiguity_group(group: Group, assembly: Assembly) -> str:
    names, more = members_named(group.members, MAX_NAMED)
    shown = ", ".join(names) + (f" and {more} more" if more else "")
    families = ", ".join(group.families)
    if group.rule_resolution == "covered":
        answer = f"covered by {', '.join(group.rule_job_groups)}"
    elif group.rule_resolution == "uncovered":
        answer = f"not covered: no job group builds {families} for {group.arch}"
    else:
        answer = "undetermined: the directory name follows no known architecture prefix"
    return (f"{group.id}: {len(group.members)} {group.arch} platform(s) declaring {families}: {shown}. "
            f"Rule answer: {answer}. Its evidence: {_by_role(group.evidence, assembly)}.")


def _answer_line(assembly: Assembly) -> str:
    """The reply's shape, shown with the alternatives rather than a filled-in answer to copy.

    Its citations are the first group's own ids for each required role, because a 7B model
    copies whatever ids an example shows, and an example citing arbitrary ids was copied
    verbatim into answers that then failed the completeness factor.
    """
    first = assembly.groups[0] if assembly.groups else None
    cites = json.dumps(_example_cites(first, assembly)) if first else "[]"
    group = first.id if first else "A"
    if assembly.kind == QUESTION_AMBIGUITY:
        example = (f'{{"group": "{group}", "reason": "<why, in under 30 words>", "covered": true or false, '
                   f'"job_group": "<the one job group that builds it, or empty>", "cite": {cites}}}')
    else:
        example = (f'{{"group": "{group}", "reason": "<why, in under 30 words>", '
                   f'"verdict": "material" or "pass-through" or "unclear", "cite": {cites}}}')
    ids = ", ".join(item.id for item in assembly.groups)
    return f'ANSWER for every group ({ids}) with: {{"action": "{ACTION_ANSWER}", "answers": [{example}, ...]}}'


def _example_cites(group: Group, assembly: Assembly) -> List[str]:
    cites = []
    for role in required_roles(assembly.question) or ("cause", "affected"):
        pool = group.evidence if role != "contract" else (*group.evidence, *assembly.context)
        items = [assembly.book.get(evidence_id) for evidence_id in pool]
        found = next((item.id for item in items if item is not None and item.role == role and item.rev != REV_BASE),
                     None)
        if found:
            cites.append(found)
    return cites


def _cite_line(assembly: Assembly) -> str:
    roles: List[str] = list(required_roles(assembly.question))
    if not roles:
        return "Cite the evidence each answer rests on."
    shared = [item.id for item in assembly.book.items if item.id in assembly.context and item.role == "contract"]
    own = [role for role in roles if role != "contract" or not shared]
    line = f"Each answer must cite at least one {', one '.join(roles)} id: the group's own {' and '.join(own)} ids"
    if "contract" in roles and shared:
        line += f", and a contract id from {', '.join(shared)}"
    return line + "."
