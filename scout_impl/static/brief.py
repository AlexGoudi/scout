"""The risk brief: stage 1's deliverable and stage 2's entire input (HLD section 4.4).

It is a file, it is versioned, and both stages validate it — stage 1 on write, stage 2 on
read — so either can run without the other. Three of its blocks earn their place by
closing a specific failure mode rather than by being nice to have:

`entities` is the closed world. A finding naming something absent from it is dropped
before the citation resolver runs, which is what stops an agent inventing a platform.

`coverage.parse` publishes how Scout read the pipeline, not just what it concluded, so a
reviewer can see which job groups Scout believed exist and whether the second parse
agreed. That is the only defence against the one failure that changes every number while
nothing errors.

`declarations_in_tree` and `platforms_in_tree` sit side by side with the named `_common`
exclusions between them, so the 287-versus-284 difference is visible in the artifact
rather than only in the document, and `kept_without_hwsku` records how many chassis
supervisors rule C4 refused to throw away. Both directions of the classification are
auditable, which is the point.

Serialization is deterministic — sorted keys, sorted sets, a caller-supplied clock and
run id — because a byte-identical brief over a pinned fixture is the static stage's
determinism test (NFR-3) and it only works if nothing here is incidental.
"""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..detectors.base import Question, Rule, Unresolved
from .schema import validate_brief

SCHEMA_VERSION = "1.0"

STATUS_COMPLETE = "complete"
STATUS_PARTIAL = "partial"
STATUS_DEGRADED = "degraded"

# What stage 2 may spend on one brief, before the per-question budgets divide it up.
DEFAULT_BUDGET = {"max_tool_calls": 60, "max_blob_reads": 30, "max_input_tokens": 120000}


@dataclass(frozen=True)
class BriefEntity:
    """One member of the closed world."""

    id: str
    kind: str
    members: int
    resolved_via: str
    source: str
    arch: str = ""
    family: str = ""

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "id": self.id,
            "kind": self.kind,
            "members": self.members,
            "resolved_via": self.resolved_via,
            "source": self.source,
        }
        if self.arch:
            payload["arch"] = self.arch
        if self.family:
            payload["family"] = self.family
        return payload


@dataclass(frozen=True)
class Brief:
    """A validated risk brief, and the bytes it serializes to."""

    payload: Dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.payload["brief"]["id"])

    @property
    def coverage(self) -> Dict[str, Any]:
        return self.payload["coverage"]

    def to_json(self) -> str:
        return json.dumps(self.payload, indent=2, sort_keys=True) + "\n"

    def canonical_json(self) -> str:
        """The brief with its per-run measurements removed. Two senses, one need.

        `static_duration_s` is wall-clock, and `id` and `measured_at` are per-run by
        construction, so three fields of the artifact are not functions of the tree. They
        are what NFR-3's byte-identical test has to look past, and they are also what would
        otherwise make the cache key of HLD section 6.4 unique per run and the cache
        useless. Everything that is a fact about the tree stays in.
        """
        payload = json.loads(json.dumps(self.payload))
        for measurement in ("id", "measured_at", "static_duration_s"):
            payload["brief"].pop(measurement, None)
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"

    def sha(self) -> str:
        """Content hash over `canonical_json`; the cache key stage 2 answers against."""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def write(self, path: Path) -> str:
        Path(path).write_text(self.to_json(), encoding="utf-8")
        return self.sha()

    def validate(self, directory: Optional[Path] = None) -> None:
        validate_brief(self.payload, directory)


@dataclass
class BriefBuilder:
    """Assembles a brief block by block, then validates it before anyone sees it."""

    repo: str
    adapter_name: str
    adapter_version: str
    rule_pack_sha: str
    mode: str
    base_sha: str
    head_sha: str
    tree_paths: int
    measured_at: str
    run_id: str
    hotspots: List[Dict[str, Any]] = field(default_factory=list)
    entities: List[BriefEntity] = field(default_factory=list)
    coverage: Dict[str, Any] = field(default_factory=dict)
    rules: Tuple[Rule, ...] = ()
    questions: Tuple[Question, ...] = ()
    unresolved: Tuple[Unresolved, ...] = ()
    budget: Dict[str, int] = field(default_factory=lambda: dict(DEFAULT_BUDGET))
    status: str = STATUS_COMPLETE
    entity_closure: bool = True
    # Omitted, not emptied, when the change touches no feature group: every brief that
    # predates the block then keeps its bytes, and with them its `sha()` cache key.
    paths_to_assess: Optional[Dict[str, Any]] = None

    def build(self, static_duration_s: float, blobs_read: int, blob_cache_hits: int = 0) -> Brief:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "brief": {
                "id": self.run_id,
                "repo": self.repo,
                "adapter": {
                    "name": self.adapter_name,
                    "version": self.adapter_version,
                    "rule_pack_sha": self.rule_pack_sha,
                },
                "mode": self.mode,
                "base_sha": self.base_sha,
                "head_sha": self.head_sha,
                "tree_paths": self.tree_paths,
                "measured_at": self.measured_at,
                "static_duration_s": round(static_duration_s, 3),
                "blobs_read": blobs_read,
                "blob_cache_hits": blob_cache_hits,
                "status": self.status,
            },
            "hotspots": list(self.hotspots),
            "entities": [entity.to_dict() for entity in self.entities],
            "coverage": dict(self.coverage),
            "rules": [rule.to_dict() for rule in self.rules],
            "questions": [question.to_dict() for question in self.questions],
            "unresolved": [item.to_dict() for item in self.unresolved],
            "entity_closure": self.entity_closure,
            "budget": dict(self.budget),
        }
        if self.paths_to_assess is not None:
            payload["paths_to_assess"] = json.loads(json.dumps(self.paths_to_assess))
        brief = Brief(payload=payload)
        brief.validate()
        _check_invariants(payload)
        return brief


def rule_pack_sha(rules: Tuple[Any, ...]) -> str:
    """A hash over the adapter's rule statements, so a brief records which pack made it."""
    digest = hashlib.sha256()
    for rule in rules:
        digest.update(f"{rule.id}\x00{rule.statement}\x00{rule.citation_kind}\x00".encode("utf-8"))
    return digest.hexdigest()[:16]


def _check_invariants(payload: Dict[str, Any]) -> None:
    """The contracts HLD section 4.4 states in prose, checked in code before the file lands.

    A schema cannot express "the components sum to the score" or "the three sets partition
    the affected set", and those two are exactly the properties a reader would otherwise
    have to take on trust.
    """
    problems: List[str] = []

    for hotspot in payload["hotspots"]:
        total = round(sum(hotspot["score_breakdown"].values()), 6)
        if abs(total - hotspot["score"]) > 1e-9:
            problems.append(
                f"hotspot {hotspot['id']}: components sum to {total} but score is {hotspot['score']}"
            )

    coverage = payload["coverage"]
    affected = set(coverage["affected"])
    partition = [set(coverage["covered"]), set(coverage["uncovered"]), set(coverage["ambiguous"])]
    if set().union(*partition) != affected:
        problems.append("coverage: covered, uncovered and ambiguous do not cover `affected` exactly")
    if sum(len(part) for part in partition) != len(affected):
        problems.append("coverage: covered, uncovered and ambiguous overlap")
    # The counting rules as one equation, so the artifact shows its own arithmetic. C3
    # takes the shared directories off and C6 puts the aliased ones on; on upstream both
    # are three, which makes the platform count coincide with the declaration count for
    # reasons that have nothing to do with each other.
    counted = (coverage["declarations_in_tree"]
               - len(coverage["excluded_as_non_platform"])
               + coverage.get("aliased_platforms", 0))
    if counted != coverage["platforms_in_tree"]:
        problems.append(
            f"coverage: {coverage['declarations_in_tree']} declarations less "
            f"{len(coverage['excluded_as_non_platform'])} exclusions plus "
            f"{coverage.get('aliased_platforms', 0)} aliases is {counted}, not "
            f"{coverage['platforms_in_tree']} platforms"
        )
    if not coverage["parse"]["loose_scan_agrees"]:
        problems.append("coverage.parse: the strict parse and the loose scan disagree")

    known = {entity["id"] for entity in payload["entities"]}
    for block, items in (("hotspots", payload["hotspots"]), ("questions", payload["questions"]),
                         ("unresolved", payload["unresolved"])):
        for item in items:
            outside = sorted(set(item.get("entities") or []) - known)
            if outside:
                problems.append(f"{block} {item['id']}: names entities outside the closed world: {outside}")
    for key in ("affected", "covered", "uncovered", "ambiguous"):
        outside = sorted(set(coverage[key]) - known)
        if outside:
            problems.append(f"coverage.{key}: names entities outside the closed world: {outside}")

    stated = {rule["id"] for rule in payload["rules"]}
    for question in payload["questions"]:
        if question["rule"] not in stated:
            problems.append(f"question {question['id']}: names rule {question['rule']!r}, which the brief omits")

    open_items = {item["id"] for item in payload["unresolved"]}
    for question in payload["questions"]:
        if question.get("unresolved") and question["unresolved"] not in open_items:
            problems.append(f"question {question['id']}: adjudicates {question['unresolved']!r}, which the brief omits")

    ambiguous = set(coverage["ambiguous"])
    for item in payload["unresolved"]:
        candidate = item.get("rule_candidate")
        if not candidate:
            continue
        unstated = sorted(set(candidate["rules"]) - stated)
        if unstated:
            problems.append(f"unresolved {item['id']}: rule candidate rests on rules the brief omits: {unstated}")
        named = [platform["entity"] for platform in candidate["platforms"]]
        if set(named) != ambiguous or len(named) != len(ambiguous):
            problems.append(f"unresolved {item['id']}: rule candidate does not answer exactly the ambiguous set")
        outside = sorted({group for platform in candidate["platforms"] for group in platform["job_groups"]} - known)
        if outside:
            problems.append(
                f"unresolved {item['id']}: rule candidate names job groups outside the closed world: {outside}"
            )
        implied = len(coverage["uncovered"]) + sum(
            1 for platform in candidate["platforms"] if platform["resolution"] == "uncovered"
        )
        if candidate["uncovered"] != implied:
            problems.append(
                f"unresolved {item['id']}: rule candidate counts {candidate['uncovered']} uncovered, "
                f"but its per-platform answers imply {implied}"
            )
        # The headline the candidate list reports and the per-platform breakdown underneath
        # it are the same claim, and used to be computed twice and disagree by six.
        for named_candidate in item["candidates"]:
            if named_candidate["rule"] == candidate.get("candidate"):
                if named_candidate["uncovered"] != candidate["uncovered"]:
                    problems.append(
                        f"unresolved {item['id']}: the {named_candidate['rule']} candidate says "
                        f"{named_candidate['uncovered']} uncovered while its own per-platform answers "
                        f"say {candidate['uncovered']}"
                    )

    related = payload.get("paths_to_assess")
    if related is not None:
        changed = {path for feature in related["features"] for path in feature["changed"]}
        if not related["features"]:
            problems.append("paths_to_assess: present but names no feature group")
        for feature in related["features"]:
            if not feature["changed"]:
                problems.append(f"paths_to_assess: feature {feature['id']!r} names no changed path")
        own = next((item["paths"] for item in related["repos"] if item["repo"] == related["repo"]), [])
        restated = sorted(changed & set(own))
        if restated:
            problems.append(f"paths_to_assess: lists changed paths as paths to assess: {restated}")

    if problems:
        raise BriefContractError(problems)


class BriefContractError(ValueError):
    """The brief satisfies its schema but violates a contract the schema cannot express."""

    def __init__(self, problems: List[str]) -> None:
        self.problems = tuple(problems)
        listed = "\n  ".join(problems)
        super().__init__(f"{len(problems)} brief contract violation(s):\n  {listed}")
