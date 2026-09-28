"""Turn one brief question into the groups the model answers for and the evidence it may cite.

Two shapes, dispatched on the question's `kind` rather than its id, because the numbering
of the brief's questions depends on which of its sets happened to be empty.

**Ambiguity**, the question over `u-001`. The brief's `rule_candidate` has already answered
every ambiguous platform, and that answer is a function of exactly two things, the
platform's architecture and the families it declares. Platforms sharing both therefore
share one answer and form one group, so the model is asked a handful of times rather than
once per platform, and the rule's answer is shown beside each group for it to confirm or
contest. The evidence is the brief's own quoted job-group citations, one declaration per
group, and the change's lines where the change touches the group.

**Materiality**, the question over the uncovered platforms. Platforms are grouped by what
reached them and what it did (`reach_signature`), so a shared file linked by thirty
platforms is one judgement, and an edit repeated across ten directories is one more. Each
group's evidence is the change as it landed, both sides of it, the link it travelled
through when it came from outside, and the declaration naming the family no job group
builds. Other files of the change ride along as context, labelled as such.

Assembly reads through the toolbox against the question's budget, so a question whose
evidence alone would overspend is truncated before the model is ever called.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..detectors.base import QUESTION_AMBIGUITY, QUESTION_MATERIALITY
from ..models import ChangeSet, FileDiff
from .budget import QuestionBudget
from .evidence import (
    MAX_CAUSE_FILES,
    ROLE_AFFECTED,
    ROLE_CAUSE,
    ROLE_CONTRACT,
    EvidenceBook,
    Reach,
    add_brief_contract,
    add_change,
    add_declaration,
    add_job_group_lines,
    add_link,
    latest_diffs,
    platform_directory,
    platform_reach,
    reach_signature,
)
from .toolbox import ToolError, Toolbox

GROUP_IDS = "ABCDEFGHJKLMNPQRSTUVWXYZ"
MAX_GROUPS = 8
MAX_CONTEXT_FILES = 2
ARCH_ORDER = {"amd64": 0, "arm64": 1, "armhf": 2}

RESOLUTION_COVERED = "covered"
RESOLUTION_UNCOVERED = "uncovered"
RESOLUTION_UNDETERMINED = "undetermined"


@dataclass(frozen=True)
class Group:
    """Platforms that get one answer between them, and the evidence shown for them."""

    id: str
    members: Tuple[str, ...]
    representative: str
    arch: str = ""
    families: Tuple[str, ...] = ()
    rule_resolution: str = ""
    rule_job_groups: Tuple[str, ...] = ()
    reach: Tuple[Reach, ...] = ()
    evidence: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "group": self.id,
            "members": list(self.members),
            "arch": self.arch,
            "families": list(self.families),
            "evidence": list(self.evidence),
        }
        if self.rule_resolution:
            payload["rule"] = {"resolution": self.rule_resolution, "job_groups": list(self.rule_job_groups)}
        if self.reach:
            payload["reached_by"] = [
                {"path": item.path, "link": item.link, "via_brief": item.via_brief} for item in self.reach
            ]
        return payload


@dataclass
class Assembly:
    """Everything one question's prompt is built from."""

    kind: str
    question: Dict[str, Any]
    groups: Tuple[Group, ...] = ()
    book: EvidenceBook = field(default_factory=EvidenceBook)
    context: Tuple[str, ...] = ()
    unexamined: Tuple[str, ...] = ()
    skipped: str = ""
    changes: Dict[str, FileDiff] = field(default_factory=dict)

    def group(self, group_id: str) -> Optional[Group]:
        return next((item for item in self.groups if item.id == group_id), None)


class BriefView:
    """Read access to a validated brief payload, indexed the ways stage 2 asks."""

    def __init__(self, payload: Dict[str, Any]) -> None:
        self.payload = payload
        self.entities = {entity["id"]: entity for entity in payload["entities"]}
        self.rules = {rule["id"]: rule for rule in payload["rules"]}
        self.unresolved = {item["id"]: item for item in payload["unresolved"]}
        self.hotspots = list(payload["hotspots"])
        self.coverage = payload["coverage"]
        self.questions = list(payload["questions"])

    @property
    def run(self) -> Dict[str, Any]:
        return self.payload["brief"]

    @property
    def job_groups(self) -> List[Dict[str, Any]]:
        """Every job group the parse found, with the family and architecture the pipeline states."""
        return [
            {"name": entity["id"].split(":", 1)[1], "id": entity["id"],
             "family": entity.get("family") or entity["id"].split(":", 1)[1],
             "arch": entity.get("arch") or "amd64", "source": entity.get("source", "")}
            for entity in self.payload["entities"] if entity["kind"] == "ci_job_group"
        ]

    def question_kind(self, question: Dict[str, Any]) -> str:
        if question.get("kind"):
            return question["kind"]
        return QUESTION_AMBIGUITY if question.get("unresolved") else QUESTION_MATERIALITY

    def rule_candidate(self, question: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        item = self.unresolved.get(question.get("unresolved") or "")
        if item is None and len(self.unresolved) == 1:
            item = next(iter(self.unresolved.values()))
        return (item or {}).get("rule_candidate")


def assemble(question: Dict[str, Any], brief: BriefView, toolbox: Toolbox, change_set: Optional[ChangeSet],
             budget: Optional[QuestionBudget] = None) -> Assembly:
    kind = brief.question_kind(question)
    if kind == QUESTION_AMBIGUITY:
        return assemble_ambiguity(question, brief, toolbox, change_set, budget)
    if kind == QUESTION_MATERIALITY:
        return assemble_materiality(question, brief, toolbox, change_set, budget)
    return Assembly(kind=kind, question=question, skipped=f"stage 2 has no way to put a {kind!r} question")


def assemble_ambiguity(question: Dict[str, Any], brief: BriefView, toolbox: Toolbox,
                       change_set: Optional[ChangeSet], budget: Optional[QuestionBudget] = None) -> Assembly:
    assembly = Assembly(kind=QUESTION_AMBIGUITY, question=question)
    candidate = brief.rule_candidate(question)
    if not candidate or not candidate.get("platforms"):
        assembly.skipped = "the brief carries no rule candidate to confirm or contest"
        return assembly

    shared: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = {}
    for platform in candidate["platforms"]:
        key = (platform["arch"], tuple(sorted(platform["families"])), platform["resolution"],
               tuple(sorted(platform["job_groups"])))
        shared.setdefault(key, []).append(platform)
    ordered = sorted(shared.items(), key=lambda item: (ARCH_ORDER.get(item[0][0], 9), item[0]))

    book = assembly.book
    families = sorted({family for key, _ in ordered for family in key[1]})
    contract = add_brief_contract(book, list(brief.rules.values()), question["rule"], families)
    if not contract:
        qualified = [group for group in brief.job_groups if group["family"] in families]
        contract = add_job_group_lines(book, toolbox, qualified, question=budget, whole_items=True)
    diffs = latest_diffs(change_set)

    groups = []
    for index, ((arch, declared, resolution, job_groups), platforms) in enumerate(ordered[:MAX_GROUPS]):
        members = tuple(sorted(platform["entity"] for platform in platforms))
        representative = members[0]
        shown: List[str] = []
        entity = brief.entities.get(representative)
        if entity is not None:
            declaration = add_declaration(book, toolbox, entity, question=budget)
            if declaration is not None:
                shown.append(declaration.id)
            directory = platform_directory(entity)
            own = [diffs[path] for path in sorted(diffs) if path.startswith(directory + "/")]
            for file_diff in own[:1]:
                shown.extend(item.id for item in add_change(book, file_diff))
        groups.append(Group(
            id=GROUP_IDS[index],
            members=members,
            representative=representative,
            arch=arch,
            families=declared,
            rule_resolution=resolution,
            rule_job_groups=tuple(job.split(":", 1)[1] if ":" in job else job for job in job_groups),
            evidence=tuple(shown),
        ))
    assembly.groups = tuple(groups)
    assembly.context = tuple(item.id for item in contract)
    assembly.unexamined = tuple(platform["entity"] for _, platforms in ordered[MAX_GROUPS:] for platform in platforms)
    return assembly


def assemble_materiality(question: Dict[str, Any], brief: BriefView, toolbox: Toolbox,
                         change_set: Optional[ChangeSet], budget: Optional[QuestionBudget] = None) -> Assembly:
    assembly = Assembly(kind=QUESTION_MATERIALITY, question=question)
    diffs = latest_diffs(change_set)
    if not diffs:
        assembly.skipped = ("no change set: a whole-tree brief has no change whose effect on these platforms "
                            "could be judged")
        return assembly

    # The families are part of the key: one shared file can reach platforms of several
    # unbuilt families, and a group speaks for its members' declarations as well as their reach.
    by_signature: Dict[Tuple[Any, ...], List[Tuple[str, List[Reach]]]] = {}
    for entity_id in sorted(question["entities"]):
        entity = brief.entities.get(entity_id)
        if entity is None or entity["kind"] != "platform":
            continue
        reached = platform_reach(toolbox, entity, diffs, brief.hotspots, question=budget)
        try:
            declared = toolbox.declared_families(entity)
        except ToolError:
            declared = ()
        key = (reach_signature(entity, reached, diffs), declared)
        by_signature.setdefault(key, []).append((entity_id, reached))
    ordered = sorted(by_signature.items(), key=lambda item: item[1][0][0])

    book = assembly.book
    groups = []
    reaching = set()
    for index, ((_, declared), members) in enumerate(ordered[:MAX_GROUPS]):
        representative, reached = members[0]
        entity = brief.entities[representative]
        shown: List[str] = []
        for reach in reached[:MAX_CAUSE_FILES]:
            reaching.add(reach.path)
            assembly.changes[reach.path] = diffs[reach.path]
            shown.extend(item.id for item in add_change(book, diffs[reach.path]))
            if reach.link:
                shown.append(add_link(book, reach.link, reach.target).id)
        declaration = add_declaration(book, toolbox, entity, question=budget)
        if declaration is not None:
            shown.append(declaration.id)
        groups.append(Group(
            id=GROUP_IDS[index],
            members=tuple(entity_id for entity_id, _ in members),
            representative=representative,
            arch=entity.get("arch", ""),
            families=declared,
            reach=tuple(reached),
            evidence=tuple(shown),
        ))

    context = []
    for path in sorted(set(diffs) - reaching)[:MAX_CONTEXT_FILES]:
        assembly.changes[path] = diffs[path]
        context.extend(item.id for item in add_change(book, diffs[path], label="elsewhere in this change"))
    context.extend(item.id for item in add_job_group_lines(book, toolbox, brief.job_groups, question=budget))
    assembly.groups = tuple(groups)
    assembly.context = tuple(context)
    assembly.unexamined = tuple(entity_id for _, members in ordered[MAX_GROUPS:] for entity_id, _ in members)
    return assembly


def required_roles(question: Dict[str, Any]) -> Tuple[str, ...]:
    known = (ROLE_CAUSE, ROLE_AFFECTED, ROLE_CONTRACT)
    return tuple(role for role in question.get("required_evidence") or () if role in known)


def members_named(members: Sequence[str], limit: int) -> Tuple[List[str], int]:
    """The first `limit` members by short name, and how many more there are."""
    names = [member.split(":", 1)[1] if ":" in member else member for member in members]
    return names[:limit], max(0, len(names) - limit)
