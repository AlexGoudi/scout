"""The coverage-gap query (FR-5), and rule C5.

Three sets, never two. An entity is **covered** when some job group builds a family it
declares — rule C5, "any declared family", which is why the index is set-valued. It is
**uncovered** when none does under any matching rule. It is **ambiguous** when the answer
depends on which matching rule is applied, and the three are disjoint and exhaustive over
the affected set.

The ambiguity is architectural rather than a gap in the implementation. PR CI builds
`marvell-prestera-arm64` and `marvell-prestera-armhf` while 12 platforms declare plain
`marvell-prestera`; `aspeed-arm64` against 5 declaring `aspeed` is the same shape. Whether
those 17 count as covered depends on each platform's architecture, which `platform_asic`
does not state. **Scout does not collapse the two answers.** On upstream at `62cfe5086`
they are 91 platforms never built under string matching against 80 under the architecture
rule, both are published, and the platforms are named either way.

The architecture answer is **not** "the ambiguous ones are covered". Six of the seventeen
are amd64 boxes declaring `marvell-prestera`, and the only prestera jobs are arm64 and
armhf, so those six are never built. Reading the rule as covering all seventeen gave 74
and under-reported the gap by six, in the flattering direction. `_architecture_resolution`
decides each platform once and both the total and the per-platform breakdown the brief
publishes are read off that one answer.

Which families are aliases is read off the pipeline rather than hardcoded: a job group
whose `PLATFORM_NAME` differs from its own name is architecture-qualified by construction,
so a new one appearing upstream lands in the ambiguous set instead of being silently
counted as never built.
"""

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from .pipeline import CoverageModel
from .platforms import Entity, EntityIndex

STRING_EQUALITY = "string-equality"
ARCHITECTURE_AWARE = "architecture-aware"

# What the architecture rule decides for one ambiguous entity. `undetermined` is a real
# third answer, not a placeholder: a directory name following no known prefix states no
# architecture, so the rule has nothing to apply and the entity counts towards neither side.
RESOLUTION_COVERED = "covered"
RESOLUTION_UNCOVERED = "uncovered"
RESOLUTION_UNDETERMINED = "undetermined"


@dataclass(frozen=True)
class CoverageResult:
    """Covered, uncovered and ambiguous over one affected set, with both candidate counts."""

    kind: str
    affected: Tuple[str, ...]
    covered: Tuple[str, ...]
    uncovered: Tuple[str, ...]
    ambiguous: Tuple[str, ...]
    matched_by: Dict[str, Tuple[str, ...]]
    ambiguous_families: Dict[str, Tuple[str, ...]]
    unknown: Tuple[str, ...]
    # What the architecture rule decides for each ambiguous entity, and which job groups
    # decide it. Computed once here so the count below and the per-platform breakdown the
    # brief publishes are the same arithmetic rather than two that agree by inspection.
    ambiguous_resolution: Dict[str, str] = field(default_factory=dict)
    ambiguous_job_groups: Dict[str, Tuple[str, ...]] = field(default_factory=dict)

    @property
    def resolution_counts(self) -> Dict[str, int]:
        """How the architecture rule splits the ambiguous set, three ways."""
        counts = {RESOLUTION_COVERED: 0, RESOLUTION_UNCOVERED: 0, RESOLUTION_UNDETERMINED: 0}
        for entity_id in self.ambiguous:
            counts[self.ambiguous_resolution.get(entity_id, RESOLUTION_UNDETERMINED)] += 1
        return counts

    @property
    def uncovered_under(self) -> Dict[str, int]:
        """The two answers the ambiguity admits, both derived from one resolution.

        Under string matching every ambiguous platform is never built, because its declared
        family is not literally a job group name. Under the architecture rule each is
        decided on its own: an amd64 `marvell-prestera` box is **not** built, because the
        only prestera jobs are arm64 and armhf. Reading the architecture answer as "all of
        them are covered" was wrong by exactly those platforms, and wrong in the flattering
        direction — it under-reported the gap.
        """
        return {
            STRING_EQUALITY: len(self.uncovered) + len(self.ambiguous),
            ARCHITECTURE_AWARE: len(self.uncovered) + self.resolution_counts[RESOLUTION_UNCOVERED],
        }

    @property
    def is_exhaustive(self) -> bool:
        """The brief's contract for the `coverage` block: the three sets partition `affected`."""
        partition = set(self.covered) | set(self.uncovered) | set(self.ambiguous)
        sizes = len(self.covered) + len(self.uncovered) + len(self.ambiguous)
        return partition == set(self.affected) and sizes == len(self.affected)


def query_coverage(
    index: EntityIndex,
    model: CoverageModel,
    affected: Optional[Iterable[str]] = None,
) -> CoverageResult:
    """Partition the affected entities into covered, uncovered and ambiguous.

    `affected` defaults to every entity in the index, which is the whole-tree figure the
    documents quote; a change set narrows it to the entities its paths reach.
    """
    literal = set(model.built_families)
    aliases = model.alias_families
    wanted = set(affected) if affected is not None else {entity.id for entity in index.entities}

    covered: List[str] = []
    uncovered: List[str] = []
    ambiguous: List[str] = []
    unknown: List[str] = []
    matched_by: Dict[str, Tuple[str, ...]] = {}
    ambiguous_families: Dict[str, Tuple[str, ...]] = {}

    for entity in index.entities:
        if entity.id not in wanted:
            continue

        hits = tuple(sorted(family for family in entity.families if family in literal))
        if hits:
            covered.append(entity.id)
            matched_by[entity.id] = hits
            continue

        candidates = tuple(sorted(family for family in entity.families if family in aliases))
        if candidates:
            ambiguous.append(entity.id)
            ambiguous_families[entity.id] = candidates
            continue

        uncovered.append(entity.id)
        if not entity.families:
            unknown.append(entity.id)

    missing = sorted(wanted - {entity.id for entity in index.entities})
    uncovered.extend(missing)
    unknown.extend(missing)

    resolution, job_groups = _architecture_resolution(index, model, ambiguous)

    return CoverageResult(
        kind=index.kind,
        affected=tuple(sorted(wanted)),
        covered=tuple(sorted(covered)),
        uncovered=tuple(sorted(uncovered)),
        ambiguous=tuple(sorted(ambiguous)),
        matched_by=matched_by,
        ambiguous_families=ambiguous_families,
        unknown=tuple(sorted(unknown)),
        ambiguous_resolution=resolution,
        ambiguous_job_groups=job_groups,
    )


def _architecture_resolution(
    index: EntityIndex,
    model: CoverageModel,
    ambiguous: Iterable[str],
) -> Tuple[Dict[str, str], Dict[str, Tuple[str, ...]]]:
    """Apply the architecture rule to each ambiguous entity. The single source of that answer.

    A job group covers an entity when it builds a family the entity declares **and** builds
    it for the entity's own architecture. An entity whose directory name follows no known
    prefix states no architecture, so the rule cannot speak for it and it is `undetermined`.
    """
    resolution: Dict[str, str] = {}
    job_groups: Dict[str, Tuple[str, ...]] = {}

    for entity_id in ambiguous:
        entity = index.by_id(entity_id)
        arch = entity.arch if entity is not None else ""
        families = entity.families if entity is not None else ()
        matching = tuple(sorted(
            group.name for group in model.job_groups
            if arch and group.arch == arch and group.family in families
        ))
        job_groups[entity_id] = matching
        if matching:
            resolution[entity_id] = RESOLUTION_COVERED
        elif arch:
            resolution[entity_id] = RESOLUTION_UNCOVERED
        else:
            resolution[entity_id] = RESOLUTION_UNDETERMINED

    return resolution, job_groups


def affected_entities(index: EntityIndex, paths: Iterable[str]) -> Tuple[str, ...]:
    """Which entities a set of changed paths reaches, in both directions.

    Outward, a path inside an entity's directory reaches that entity. Inward — rule C6 —
    a path reaches every entity owning a symlink that resolves to it, which is the half a
    tree listing makes invisible and the half a change to a shared directory needs. The
    two are a union rather than a fallback: a file can be both inside one platform and the
    link target of a dozen others, and before C6 that change reported one.
    """
    wanted = [path for path in paths]
    reached = set()
    for path in wanted:
        entity = index.owning(path)
        if entity is not None:
            reached.add(entity.id)
        reached.update(index.linking_into(path))
    reached.update(index.reaching(wanted))
    return tuple(sorted(reached))


def families_of(index: EntityIndex, ids: Iterable[str]) -> Tuple[str, ...]:
    """Every family the named entities declare, in stable order."""
    wanted = set(ids)
    families = {family for entity in index.entities if entity.id in wanted for family in entity.families}
    return tuple(sorted(families))


def entities_declaring(index: EntityIndex, family: str) -> Tuple[Entity, ...]:
    return tuple(entity for entity in index.entities if family in entity.families)
