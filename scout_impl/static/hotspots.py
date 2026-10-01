"""Hotspot ranking, with a score breakdown that sums to the score.

Four signals, each contributing an explicit component: the path class of the changed
file, how many entities it reaches, how much of what it reaches PR CI never builds, and
how much of that is undecidable. The weights below are the whole model — there is no
hidden term — and `score` is the sum of the rounded components rather than a separately
rounded total, so the schema's "components sum to `score`" contract holds exactly rather
than to within a rounding error. A ranking nobody can audit is a ranking nobody trusts.

Two choices worth defending. Path class contributes on the adapter's own `rank` scale
rather than on a per-repo table here, so a repository that declares a different number of
classes ranks on the same 0-to-weight interval without the core learning its names. And
entity fan-out saturates logarithmically: a change reaching 200 platforms is meaningfully
worse than one reaching 20, but not ten times worse, and a linear term would let one
sweeping vendor-wide edit crowd every other hotspot out of a capped list.
"""

import math
from dataclasses import dataclass, replace
from typing import Dict, Iterable, List, Sequence, Tuple

from ..models import FileDiff
from ..repos.base import PathClass
from .coverage import CoverageResult, affected_entities
from .platforms import EntityIndex

WEIGHT_PATH_CLASS = 0.30
WEIGHT_ENTITY_FANOUT = 0.20
WEIGHT_COVERAGE_GAP = 0.30
WEIGHT_AMBIGUITY = 0.20

# Fan-out above this adds nothing; chosen as roughly a large vendor's platform count, so
# "reaches one vendor's whole line" is already at the top of the scale.
FANOUT_SATURATION = 60

SCORE_PRECISION = 3


@dataclass(frozen=True)
class Hotspot:
    """One changed path, ranked, with the reason for every point of its score."""

    id: str
    path: str
    path_class: str
    change_kind: str
    score: float
    score_breakdown: Dict[str, float]
    entities: Tuple[str, ...]

    def to_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "path": self.path,
            "path_class": self.path_class,
            "change_kind": self.change_kind,
            "score": self.score,
            "score_breakdown": dict(self.score_breakdown),
            "entities": list(self.entities),
        }


def rank_hotspots(
    files: Sequence[FileDiff],
    index: EntityIndex,
    coverage: CoverageResult,
    class_count: int,
    limit: int = 10,
) -> Tuple[Hotspot, ...]:
    """Score every changed path and return the top `limit`, highest first.

    A path outside the entity root reaches nothing, yet may still be the widest change in
    the set; that is what the path-class component is for.
    """
    uncovered = set(coverage.uncovered)
    ambiguous = set(coverage.ambiguous)

    scored: List[Hotspot] = []
    for file_diff in files:
        reached = affected_entities(index, [file_diff.path])
        components = {
            "path_class": _round(WEIGHT_PATH_CLASS * _class_weight(file_diff.path_class, class_count)),
            "entity_fanout": _round(WEIGHT_ENTITY_FANOUT * _fanout_weight(len(reached))),
            "coverage_gap": _round(WEIGHT_COVERAGE_GAP * _share(reached, uncovered)),
            "ambiguity": _round(WEIGHT_AMBIGUITY * _share(reached, ambiguous)),
        }
        scored.append(
            Hotspot(
                id="",
                path=file_diff.path,
                path_class=file_diff.path_class.id,
                change_kind=file_diff.change_type,
                score=_round(sum(components.values())),
                score_breakdown=components,
                entities=_entity_ids(index, reached),
            )
        )

    scored.sort(key=lambda item: (-item.score, item.path))
    return tuple(replace(item, id=f"h-{position:03d}") for position, item in enumerate(scored[:limit], start=1))


def _entity_ids(index: EntityIndex, reached: Iterable[str]) -> Tuple[str, ...]:
    ids = [f"{index.kind}:{item}" for item in reached]
    for item in reached:
        member = index.by_id(item)
        if member is not None:
            ids.extend(f"{index.family_kind}:{family}" for family in member.families)
    return tuple(sorted(set(ids)))


def _class_weight(path_class: PathClass, class_count: int) -> float:
    """1.0 for the widest blast radius, falling linearly to the narrowest class."""
    if class_count <= 1:
        return 1.0
    return max(0.0, 1.0 - (path_class.rank - 1) / (class_count - 1))


def _fanout_weight(count: int) -> float:
    if count <= 0:
        return 0.0
    return min(1.0, math.log1p(count) / math.log1p(FANOUT_SATURATION))


def _share(reached: Sequence[str], subset: set) -> float:
    if not reached:
        return 0.0
    return len([item for item in reached if item in subset]) / len(reached)


def _round(value: float) -> float:
    return round(value, SCORE_PRECISION)
