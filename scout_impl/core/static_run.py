"""Drive one static run: resolve the change set, analyze the tree, emit the brief.

The one piece of sequencing worth stating. The brief is built against the **head** tree,
and the change set only narrows which entities are affected — so a run with no change set
is legitimate and produces the whole-tree figures, which is what the conformance suite
and the coverage narrative both want. A run with one produces the same brief scoped to
what the change reaches. Nothing else differs between the two paths, which is why the
figures in a pull request's brief can be compared against the tree-wide ones without
wondering whether a different code path produced them.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..ingest import resolve
from ..models import ChangeSet, ChangeSetSpec
from ..repos import RepoAdapter
from ..source import RepoSource
from ..static.brief import Brief
from ..static.engine import MODE_TREE, StaticResult, analyze, build_brief

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StaticRun:
    """One static run: what it analyzed, what it produced, and what it cost."""

    brief: Brief
    result: StaticResult
    change_set: Optional[ChangeSet]

    @property
    def duration_s(self) -> float:
        return self.result.duration_s

    @property
    def blobs_read(self) -> int:
        return self.result.blobs_read


def run_static(
    source: RepoSource,
    repo: str,
    rev: str,
    adapter: RepoAdapter,
    spec: Optional[ChangeSetSpec] = None,
    mode: str = MODE_TREE,
    run_id: Optional[str] = None,
    measured_at: Optional[str] = None,
    hotspot_limit: int = 10,
) -> StaticRun:
    """Analyze `rev`, optionally scoped to a change set, and return a validated brief."""
    change_set = resolve(spec, source, adapter) if spec is not None else None
    result = analyze(source, rev, adapter, change_set=change_set, hotspot_limit=hotspot_limit)

    brief = build_brief(
        result,
        repo=repo,
        base_sha=change_set.base_sha if change_set else "",
        head_sha=rev,
        mode=spec.mode if spec is not None else mode,
        run_id=run_id,
        measured_at=measured_at,
    )
    logger.info(
        "Static stage over %s at %s: %d path(s), %d blob read(s), %d cache hit(s), %.3fs",
        repo,
        rev[:9],
        result.tree_paths,
        result.blobs_read,
        result.blob_cache_hits,
        result.duration_s,
    )
    return StaticRun(brief=brief, result=result, change_set=change_set)


def write_brief(run: StaticRun, path: Path) -> str:
    """Write the brief and return its sha, which is stage 2's cache key (HLD section 6.4)."""
    sha = run.brief.write(Path(path))
    logger.info("Wrote %s (%s)", path, sha[:12])
    return sha
