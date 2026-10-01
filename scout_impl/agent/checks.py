"""The constraints that hold the model to the brief, applied to what it returned.

Nothing here asks the model to behave. Each check reads an answer the model already gave
and passes it, drops it or marks it, and says which and why, and every firing is counted
so a report can say how often the model needed catching. They run in a fixed order, and
the first to fail decides what an answer is recorded as having failed:

1. **Question binding.** The answer names a group the prompt handed out. Checked in the
   runner, because it is about the reply as a whole.
2. **Entity closure.** Every platform- or job-group-shaped name in the answer is one the
   brief or the question's evidence names. A name found only in the model's text is an
   entity it invented, and the answer is dropped whole rather than edited.
3. **Cited job group.** A "covered" claim names one job group, and it must be one the
   parse found, building a family the platforms declare, for their architecture. This is
   the check that catches a right answer with an invented reason.
4. **Citation.** Every evidence id the answer cites exists and re-reads, at its revision,
   to exactly the quote it carries.
5. **Rule consistency.** An answer that survived all of that and still disagrees with the
   brief's `rule_candidate` is kept, marked `contested`, and never overrides the rule.
"""

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .assemble import RESOLUTION_COVERED, RESOLUTION_UNCOVERED, Group
from .budget import QuestionBudget
from .evidence import ORIGIN_LINK, ORIGIN_LISTING, Evidence, EvidenceBook
from .protocol import AnswerItem
from .toolbox import ToolError, Toolbox

CHECK_QUESTION_BINDING = "question_binding"
CHECK_ENTITY_CLOSURE = "entity_closure"
CHECK_CITED_JOB_GROUP = "cited_job_group"
CHECK_CITATION = "citation"
CHECK_RULE_CONSISTENCY = "rule_consistency"
CHECKS = (CHECK_QUESTION_BINDING, CHECK_ENTITY_CLOSURE, CHECK_CITED_JOB_GROUP, CHECK_CITATION,
          CHECK_RULE_CONSISTENCY)

OUTCOME_ANSWERED = "answered"
OUTCOME_CONFIRMED = "confirmed"
OUTCOME_CONTESTED = "contested"
OUTCOME_UNCONFIRMED = "unconfirmed"
OUTCOME_DROPPED = "dropped"
OUTCOME_UNANSWERED = "unanswered"
OUTCOME_UNEXAMINED = "unexamined"

ARCHITECTURES = ("amd64", "arm64", "armhf", "x86_64")

_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_PLATFORM_SHAPE = re.compile(r"^(x86_64|arm64|armhf)-[a-z0-9][a-z0-9_.-]*$", re.IGNORECASE)


class ClosedWorld:
    """The names a question's answers may use: the brief's entities, and what its evidence showed.

    Entity closure is about invention, not about vocabulary. A shared directory such as
    `x86_64-arista_common` is not an entity, but a change to it is the cause the model was
    shown, so naming it invents nothing; names that appear in the question's own evidence
    are therefore allowed alongside the brief's entities.
    """

    def __init__(self, entities: Sequence[Dict[str, Any]], shown: Sequence[str] = ()) -> None:
        self.ids = {entity["id"] for entity in entities}
        self.platform_names: Set[str] = set()
        self.job_groups: Dict[str, Dict[str, str]] = {}
        self.families: Set[str] = set()
        for entity in entities:
            name = entity["id"].split(":", 1)[1] if ":" in entity["id"] else entity["id"]
            if entity["kind"] == "platform":
                self.platform_names.add(name.split("/")[-1].lower())
            elif entity["kind"] == "ci_job_group":
                family = entity.get("family") or name
                self.job_groups[name.lower()] = {"family": family, "arch": entity.get("arch") or "amd64"}
                self.families.add(family.lower())
            elif entity["kind"] == "asic_family":
                self.families.add(name.lower())
        self.shown = {token.strip("._-").lower() for text in shown for token in _TOKEN.findall(text)}

    def outside(self, *texts: str) -> List[str]:
        """Platform- and job-group-shaped names in `texts` that nothing the model was given names."""
        found = set()
        for text in texts:
            for raw in _TOKEN.findall(text or ""):
                token = raw.strip("._-").lower()
                if not token or token in self.shown:
                    continue
                if _PLATFORM_SHAPE.match(token) and token not in self.platform_names:
                    found.add(token)
                elif self._job_group_shaped(token) and token not in self.job_groups:
                    found.add(token)
        return sorted(found)

    def _job_group_shaped(self, token: str) -> bool:
        family, _, arch = token.rpartition("-")
        return bool(family) and arch in ARCHITECTURES and family in self.families


def entity_closure(item: AnswerItem, world: ClosedWorld) -> Optional[str]:
    """None when the answer names only what it was given; else the names it invented."""
    named = [item.reason]
    if item.job_group:
        if item.job_group not in world.job_groups:
            return f"names job group {item.job_group!r}, which is not in the brief"
        named.append(item.job_group)
    outside = world.outside(*named)
    if outside:
        return f"names {', '.join(outside)}, which neither the brief nor the evidence contains"
    return None


def cited_job_group(item: AnswerItem, group: Group, world: ClosedWorld) -> Optional[str]:
    """None unless the answer claims coverage through a group that does not build these platforms."""
    if not item.covered:
        return None
    if not item.job_group:
        return "claims the platforms are covered but names no job group"
    spec = world.job_groups.get(item.job_group)
    if spec is None:
        return f"claims coverage by {item.job_group!r}, which the pipeline parse did not find"
    problems = []
    if spec["family"] not in group.families:
        problems.append(f"it builds {spec['family']}, which these platforms do not declare")
    if spec["arch"] != group.arch:
        problems.append(f"it builds {spec['arch']} and these platforms are {group.arch or 'of unknown architecture'}")
    if problems:
        return f"claims coverage by {item.job_group}, but {' and '.join(problems)}"
    return None


def rule_consistency(item: AnswerItem, group: Group) -> Tuple[str, str]:
    """`confirmed` or `contested` against the rule candidate, with the two answers side by side."""
    if group.rule_resolution not in (RESOLUTION_COVERED, RESOLUTION_UNCOVERED):
        return OUTCOME_UNCONFIRMED, "the rule gives no answer for this group, so nothing can confirm this one"
    rule_covered = group.rule_resolution == RESOLUTION_COVERED
    if bool(item.covered) == rule_covered:
        return OUTCOME_CONFIRMED, ""
    rule = (f"covered by {', '.join(group.rule_job_groups)}" if rule_covered else "not covered")
    agent = f"covered by {item.job_group}" if item.covered else "not covered"
    return OUTCOME_CONTESTED, f"the rule says {rule}; the model says {agent}"


@dataclass
class CitationResolver:
    """Re-reads cited evidence at its revision and checks it still says what it quotes.

    Re-reads go through the toolbox, so they are charged like any other read, and a blob the
    evidence assembly already fetched costs nothing twice. Each item is verified once.
    """

    toolbox: Toolbox
    book: EvidenceBook

    def __post_init__(self) -> None:
        self._verified: Dict[str, Optional[str]] = {}

    def resolve(self, cites: Sequence[str], question: Optional[QuestionBudget] = None
                ) -> Tuple[List[Evidence], Optional[str]]:
        """The cited evidence, each re-read; or, if any fails, nothing and the first reason."""
        if not cites:
            return [], "cites no evidence"
        resolved = []
        for evidence_id in cites:
            item = self.book.get(evidence_id)
            if item is None:
                return [], f"cites {evidence_id}, which is not in the evidence it was shown"
            problem = self.verify(item, question)
            if problem:
                return [], f"{evidence_id} does not re-resolve: {problem}"
            resolved.append(item)
        return resolved, None

    def verify(self, item: Evidence, question: Optional[QuestionBudget] = None) -> Optional[str]:
        if item.id not in self._verified:
            self._verified[item.id] = self._reread(item, question)
        return self._verified[item.id]

    def _reread(self, item: Evidence, question: Optional[QuestionBudget]) -> Optional[str]:
        try:
            if item.origin == ORIGIN_LISTING:
                entries, _ = self.toolbox.list_tree("" if item.path == "." else item.path, item.rev)
                expected = _rows(item.quote)
                actual = [f"{entry.mode} {entry.path}" for entry in entries][:len(expected)]
                return None if actual == expected else f"the listing of {item.path} at {item.rev} has changed"
            if item.origin == ORIGIN_LINK:
                target = self.toolbox.content(item.path, item.rev, question, purpose="resolve").strip()
                return None if target == item.quote.strip() else f"{item.path} now links to {target!r}"
            rows = self.toolbox.lines(item.path, item.rev, question, purpose="resolve")
        except ToolError as error:
            return str(error)
        if item.line_start < 1 or item.line_end > len(rows) or item.line_start > item.line_end:
            return f"{item.path} at {item.rev} has {len(rows)} line(s), not {item.line_start}-{item.line_end}"
        actual = [row.rstrip() for row in rows[item.line_start - 1:item.line_end]]
        expected = [row.rstrip() for row in _rows(item.quote)]
        if actual != expected:
            return f"{item.path}:{item.span} at {item.rev} does not read as quoted"
        return None


def _rows(quote: str) -> List[str]:
    """A quote's lines, exactly as it was joined. `splitlines` would drop a trailing blank line,
    and a quoted run of added lines can end in one."""
    return quote.split("\n") if quote else []


def premises(group: Group, context: Sequence[str]) -> Set[str]:
    """The evidence the rule's own answer rests on: citing only these is not counter-evidence."""
    return set(group.evidence) | set(context)
