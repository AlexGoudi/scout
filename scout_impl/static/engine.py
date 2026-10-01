"""The static analyzer engine: run an adapter over a tree and produce a brief.

Substages in a fixed order — classify paths, resolve entities,
model coverage, query the gap, rank hotspots, synthesize rules and questions — then the
feature groups the change touches (`related.py`), with the
whole thing timed and its blob reads counted, because `static_duration_s` and
`blobs_read` are fields in the artifact rather than log lines (NFR-12).

The engine knows nothing about SONiC. It reads the adapter's `entity_model` to learn how
that repository declares its entities, its `coverage_spec` to learn where its CI says what
it builds, its `rules` for the invariants to state, and its `detectors` for what to ask.
An adapter that supplies none of those still gets a run: it produces a brief with an empty
coverage block and no questions, which is the honest output for a repository Scout can
ingest but cannot yet analyze, and is not the same thing as a crash.
"""

import posixpath
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..detectors import detectors_for
from ..detectors.base import Question, Rule, Synthesis, Unresolved
from ..models import ChangeSet, FileDiff
from ..repos import RepoAdapter
from .brief import Brief, BriefBuilder, BriefEntity, rule_pack_sha
from .coverage import CoverageResult, affected_entities, query_coverage
from .extract import build_coverage, build_index
from .hotspots import Hotspot, rank_hotspots
from .pipeline import CoverageModel
from .platforms import EntityIndex
from .related import Related, load_feature_map
from .treeindex import TreeIndex

ADAPTER_VERSION = "1.0"
MODE_TREE = "tree"


@dataclass(frozen=True)
class StaticResult:
    """Everything stage 1 established, before it is serialized into a brief."""

    adapter: RepoAdapter
    rev: str
    tree_paths: int
    index: Optional[EntityIndex]
    model: Optional[CoverageModel]
    coverage: Optional[CoverageResult]
    hotspots: Tuple[Hotspot, ...]
    synthesis: Synthesis
    duration_s: float
    blobs_read: int
    blob_cache_hits: int
    related: Optional[Related] = None

    @property
    def rules(self) -> Tuple[Rule, ...]:
        return self.synthesis.rules

    @property
    def questions(self) -> Tuple[Question, ...]:
        return self.synthesis.questions

    @property
    def unresolved(self) -> Tuple[Unresolved, ...]:
        return self.synthesis.unresolved


def analyze(
    source: Any,
    rev: str,
    adapter: RepoAdapter,
    change_set: Optional[ChangeSet] = None,
    hotspot_limit: int = 10,
) -> StaticResult:
    """Run the deterministic stage over one tree. No model, no credential, no new fetch."""
    started = time.monotonic()
    tree = TreeIndex(source, rev)

    index = build_index(tree, adapter.entity_model) if adapter.entity_model else None
    model = build_coverage(tree, adapter.coverage_spec) if adapter.coverage_spec else None

    files = _changed_files(change_set)
    coverage = None
    if index is not None and model is not None:
        affected = affected_entities(index, [item.path for item in files]) if change_set else None
        coverage = query_coverage(index, model, affected)

    hotspots: Tuple[Hotspot, ...] = ()
    if index is not None and coverage is not None and files:
        hotspots = rank_hotspots(files, index, coverage, len(adapter.path_classes), limit=hotspot_limit)

    synthesis = Synthesis()
    if index is not None and model is not None and coverage is not None:
        for detector in detectors_for(adapter):
            synthesis = _merge(synthesis, detector.synthesize(index, model, coverage, adapter.rules))

    related = load_feature_map().related(adapter.name, [item.path for item in files]) if files else None

    return StaticResult(
        adapter=adapter,
        rev=rev,
        tree_paths=tree.total_paths,
        index=index,
        model=model,
        coverage=coverage,
        hotspots=hotspots,
        synthesis=synthesis,
        duration_s=time.monotonic() - started,
        blobs_read=tree.blob_reads,
        blob_cache_hits=tree.cache_hits,
        related=related,
    )


def build_brief(
    result: StaticResult,
    repo: str,
    base_sha: str = "",
    head_sha: str = "",
    mode: str = MODE_TREE,
    run_id: Optional[str] = None,
    measured_at: Optional[str] = None,
) -> Brief:
    """Serialize a static result as a validated risk brief."""
    entities = _entities(result)
    builder = BriefBuilder(
        repo=repo,
        adapter_name=result.adapter.name,
        adapter_version=ADAPTER_VERSION,
        rule_pack_sha=rule_pack_sha(result.adapter.rules),
        mode=mode,
        base_sha=base_sha,
        head_sha=head_sha or result.rev,
        tree_paths=result.tree_paths,
        measured_at=measured_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        run_id=run_id or str(uuid.uuid4()),
        hotspots=[hotspot.to_dict() for hotspot in result.hotspots],
        entities=entities,
        coverage=_coverage_block(result),
        rules=result.rules,
        questions=result.questions,
        unresolved=result.unresolved,
        paths_to_assess=result.related.to_dict() if result.related and not result.related.empty else None,
    )
    return builder.build(result.duration_s, result.blobs_read, result.blob_cache_hits)


def _changed_files(change_set: Optional[ChangeSet]) -> List[FileDiff]:
    """One entry per path in the change set, keeping the widest-blast-radius class seen."""
    if change_set is None:
        return []
    by_path: Dict[str, FileDiff] = {}
    for commit in change_set.commits:
        for file_diff in commit.files:
            existing = by_path.get(file_diff.path)
            if existing is None or file_diff.path_class.rank < existing.path_class.rank:
                by_path[file_diff.path] = file_diff
    return [by_path[path] for path in sorted(by_path)]


def _coverage_block(result: StaticResult) -> Dict[str, Any]:
    if result.index is None or result.model is None or result.coverage is None:
        return _empty_coverage(result)

    index, model, coverage = result.index, result.model, result.coverage
    return {
        "model": model.model,
        "job_groups": len(model.names),
        "job_group_names": list(model.names),
        "parse": model.as_parse_record(),
        "declarations_in_tree": index.declaration_count,
        "platforms_in_tree": len(index.entities),
        "excluded_as_non_platform": list(index.excluded),
        # C6, published beside C3's exclusions so the two adjustments to the declaration
        # count are visible separately rather than cancelling out into a coincidence.
        "aliased_platforms": len(index.aliases),
        "aliased_as_platform": list(index.aliases),
        "kept_without_hwsku": len(index.kept_without_hwsku),
        "symlinked_declarations": len(index.symlink_declarations),
        "unresolved_declarations": len(index.unresolved),
        "multi_family_declarations": len(index.multi_family),
        "affected": [f"{index.kind}:{item}" for item in coverage.affected],
        "covered": [f"{index.kind}:{item}" for item in coverage.covered],
        "uncovered": [f"{index.kind}:{item}" for item in coverage.uncovered],
        "ambiguous": [f"{index.kind}:{item}" for item in coverage.ambiguous],
    }


def _empty_coverage(result: StaticResult) -> Dict[str, Any]:
    """The honest block for an adapter with no coverage model: empty, not invented."""
    return {
        "model": "none",
        "job_groups": 0,
        "job_group_names": [],
        "parse": {"scope": "none", "strict": False, "loose_scan_agrees": True,
                  "limitations": [f"adapter {result.adapter.name} declares no CI coverage model"]},
        "declarations_in_tree": result.index.declaration_count if result.index else 0,
        "platforms_in_tree": len(result.index.entities) if result.index else 0,
        "excluded_as_non_platform": list(result.index.excluded) if result.index else [],
        "kept_without_hwsku": len(result.index.kept_without_hwsku) if result.index else 0,
        "affected": [],
        "covered": [],
        "uncovered": [],
        "ambiguous": [],
    }


def _entities(result: StaticResult) -> List[BriefEntity]:
    """The closed world: everything a stage-2 finding is allowed to name, and nothing else."""
    if result.index is None:
        return []

    index = result.index
    named: Dict[str, BriefEntity] = {}
    affected = set(result.coverage.affected) if result.coverage else {item.id for item in index.entities}
    families = index.families

    for entity in index.entities:
        if entity.id not in affected:
            continue
        named[f"{index.kind}:{entity.id}"] = BriefEntity(
            id=f"{index.kind}:{entity.id}",
            kind=index.kind,
            members=1,
            resolved_via=entity.resolved_via,
            source=entity.declaration_path,
            # A convention read off the directory name, not a declaration (rule BI-R4).
            # It is in the brief because it is the evidence `u-001` is adjudicated with.
            arch=entity.arch,
        )
        for family in entity.families:
            key = f"{index.family_kind}:{family}"
            named.setdefault(key, BriefEntity(
                id=key,
                kind=index.family_kind,
                members=len(families.get(family, ())),
                resolved_via="blob",
                source=entity.resolved_path or entity.declaration_path,
            ))

    # Only a tree that groups its entities by vendor has vendor entities to name; a
    # file-shaped entity model has none, and inventing an empty one would put a
    # meaningless id into the closed world.
    for vendor in sorted({entity.vendor for entity in index.entities if entity.id in affected and entity.vendor}):
        key = f"vendor:{vendor}"
        owned = [item for item in index.entities if item.vendor == vendor]
        named[key] = BriefEntity(
            id=key,
            kind="vendor",
            members=len(owned),
            resolved_via="tree",
            source=posixpath.dirname(owned[0].directory) if owned else vendor,
        )

    if result.model is not None:
        for group in result.model.job_groups:
            key = f"ci_job_group:{group.name}"
            named[key] = BriefEntity(
                id=key,
                kind="ci_job_group",
                members=len(families.get(group.name, ())),
                resolved_via="blob",
                source=result.model.path,
                arch=group.arch,
                family=group.family,
            )
            alias = f"{index.family_kind}:{group.family}"
            if group.is_arch_qualified and alias not in named:
                named[alias] = BriefEntity(
                    id=alias,
                    kind=index.family_kind,
                    members=len(families.get(group.family, ())),
                    resolved_via="blob",
                    source=result.model.path,
                )

    return [named[key] for key in sorted(named)]


def _merge(left: Synthesis, right: Synthesis) -> Synthesis:
    return Synthesis(
        rules=_unique(left.rules + right.rules),
        questions=_unique(left.questions + right.questions),
        unresolved=_unique(left.unresolved + right.unresolved),
        triggered=left.triggered or right.triggered,
    )


def _unique(items: Sequence[Any]) -> Tuple[Any, ...]:
    seen = {}
    for item in items:
        seen.setdefault(item.id, item)
    return tuple(seen.values())
