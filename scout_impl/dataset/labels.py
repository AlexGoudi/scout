"""Outcome labels. They are never features: each one depends on commits that land later.

Every label records when its event landed, so the split stage can count only events that
landed before a split ends.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from ..mining.gitio import Git
from .history import DAY, CommitFacts
from .shards import write_bytes_atomic

REVERT_WINDOWS_DAYS = (7, 30, 90)
BLAME_VERSION = "1"
# 2: bug_introducing is unknown, not false, for a commit touching no SZZ-eligible file.
LABELS_VERSION = "2"
# A fix deleting more lines than this is most likely a refactor or a rewrite, and blaming
# it would mark every author of the old code as having introduced the bug.
SZZ_MAX_DELETED_LINES = 1000
TRIVIAL_CHARACTERS = frozenset("{}()[];,")
COMMENT_PREFIXES = ("//", "/*", "*/", "* ", "# ", "--")

Progress = Callable[[str], None]


@dataclass(frozen=True)
class RevertLink:
    target: int
    revert: int
    method: str
    nested: bool


@dataclass(frozen=True)
class BugLink:
    introducer: int
    fix: int
    fix_count: int


def link_reverts(facts: Sequence[CommitFacts]) -> tuple[dict[int, RevertLink], dict[str, int]]:
    """Map each reverted commit to its earliest revert: by trailer sha, else by PR number."""
    by_sha = {fact.sha: fact.index for fact in facts}
    by_pr: dict[int, int] = {}
    links: dict[int, RevertLink] = {}
    stats = {"reverts": 0, "linked_by_sha": 0, "linked_by_pr": 0, "unlinked": 0, "nested": 0}
    for fact in facts:
        if fact.is_revert:
            stats["reverts"] += 1
            target, method = None, None
            candidate = by_sha.get(fact.reverts_sha or "")
            if candidate is not None and candidate < fact.index:
                target, method = candidate, "sha"
            elif fact.reverts_pr is not None and by_pr.get(fact.reverts_pr, fact.index) < fact.index:
                target, method = by_pr[fact.reverts_pr], "pr"
            if target is None or method is None:
                stats["unlinked"] += 1
            else:
                stats[f"linked_by_{method}"] += 1
                nested = facts[target].is_revert
                stats["nested"] += nested
                if target not in links:
                    links[target] = RevertLink(target=target, revert=fact.index, method=method, nested=nested)
        if fact.pr_number is not None:
            by_pr.setdefault(fact.pr_number, fact.index)
    return links, stats


class BlameCache:
    """Blamed commits per line, keyed by (parent, path, ranges); ``root=None`` disables it."""

    def __init__(self, root: str | Path | None) -> None:
        self.directory = Path(root) / "blame" / f"v{BLAME_VERSION}" if root else None

    def _path(self, parent: str, path: str, ranges: tuple[tuple[int, int], ...]) -> Path:
        assert self.directory is not None
        digest = hashlib.sha256(f"{parent}\x00{path}\x00{ranges!r}".encode("utf-8", "surrogateescape")).hexdigest()
        return self.directory / parent[:2] / f"{parent}-{digest[:24]}.json"

    def get(self, parent: str, path: str, ranges: tuple[tuple[int, int], ...]) -> list[str] | None:
        if self.directory is None:
            return None
        try:
            value = json.loads(self._path(parent, path, ranges).read_text())
        except (OSError, ValueError):
            return None
        return value if isinstance(value, list) else None

    def put(self, parent: str, path: str, ranges: tuple[tuple[int, int], ...], shas: list[str]) -> None:
        if self.directory is not None:
            write_bytes_atomic(self._path(parent, path, ranges), json.dumps(shas).encode("ascii"))


def szz(
    git: Git,
    facts: Sequence[CommitFacts],
    *,
    cache: BlameCache,
    workers: int = 1,
    progress: Progress | None = None,
) -> tuple[dict[int, BugLink], dict[str, int]]:
    """Blame, at its parent, every line a fix-like commit removed or changed."""
    say = progress or (lambda message: None)
    by_sha = {fact.sha: fact.index for fact in facts}
    jobs = []
    stats = {"fix_commits": 0, "fix_commits_blamed": 0, "fix_commits_too_large": 0, "blame_calls": 0}
    for fact in facts:
        if not fact.fix_like or fact.parent is None:
            continue
        stats["fix_commits"] += 1
        if not fact.szz_targets:
            continue
        if fact.szz_deleted_lines > SZZ_MAX_DELETED_LINES:
            stats["fix_commits_too_large"] += 1
            continue
        stats["fix_commits_blamed"] += 1
        jobs.extend((fact.index, fact.parent, path, ranges) for path, ranges in fact.szz_targets)
    stats["blame_calls"] = len(jobs)
    say(f"szz: {stats['fix_commits']} fix-like commits, {len(jobs)} blame calls")

    def run(job: tuple[int, str, str, tuple[tuple[int, int], ...]]) -> tuple[int, list[str]]:
        index, parent, path, ranges = job
        shas = cache.get(parent, path, ranges)
        if shas is None:
            shas = [sha for sha, text in git.blame(parent, path, ranges) if _meaningful(text)]
            cache.put(parent, path, ranges, shas)
        return index, shas

    fixes_of: dict[int, set[int]] = {}
    lines = attributed = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        for done, (fix, shas) in enumerate(executor.map(run, jobs), 1):
            for sha in shas:
                lines += 1
                introducer = by_sha.get(sha)
                if introducer is None or introducer >= fix:
                    continue
                attributed += 1
                fixes_of.setdefault(introducer, set()).add(fix)
            if done % 2000 == 0:
                say(f"szz: {done}/{len(jobs)} blame calls")
    stats["blamed_lines"] = lines
    stats["blamed_lines_on_first_parent"] = attributed
    links = {
        introducer: BugLink(introducer=introducer, fix=min(fixes), fix_count=len(fixes))
        for introducer, fixes in fixes_of.items()
    }
    return links, stats


def commit_labels(
    facts: Sequence[CommitFacts],
    reverts: dict[int, RevertLink],
    bugs: dict[int, BugLink] | None,
) -> list[dict[str, Any]]:
    """The labels block of every commit, as of the snapshot, with event landing times."""
    rows = []
    for fact in facts:
        row: dict[str, Any] = {}
        link = reverts.get(fact.index)
        if fact.is_merge:
            row.update(
                reverted=None, reverted_by=None, reverted_landed=None, revert_lead_days=None, revert_link=None,
                revert_nested=None,
            )
            row.update({f"reverted_within_{days}d": None for days in REVERT_WINDOWS_DAYS})
        elif link is None:
            row.update(
                reverted=False, reverted_by=None, reverted_landed=None, revert_lead_days=None, revert_link=None,
                revert_nested=False,
            )
            row.update({f"reverted_within_{days}d": False for days in REVERT_WINDOWS_DAYS})
        else:
            revert = facts[link.revert]
            lead = (revert.landed - fact.landed) / DAY
            row.update(
                reverted=True,
                reverted_by=revert.sha,
                reverted_landed=revert.landed,
                revert_lead_days=round(lead, 3),
                revert_link=link.method,
                revert_nested=link.nested,
            )
            row.update({f"reverted_within_{days}d": lead <= days for days in REVERT_WINDOWS_DAYS})

        bug = bugs.get(fact.index) if bugs is not None else None
        if bugs is None or fact.is_merge or fact.submodule_only or not fact.szz_eligible:
            row.update(bug_introducing=None, fixed_by=None, fixed_landed=None, fix_lead_days=None, fix_count=None)
        elif bug is None:
            row.update(bug_introducing=False, fixed_by=None, fixed_landed=None, fix_lead_days=None, fix_count=0)
        else:
            fix = facts[bug.fix]
            row.update(
                bug_introducing=True,
                fixed_by=fix.sha,
                fixed_landed=fix.landed,
                fix_lead_days=round((fix.landed - fact.landed) / DAY, 3),
                fix_count=bug.fix_count,
            )
        rows.append(row)
    return rows


def _meaningful(text: str) -> bool:
    stripped = text.strip()
    if not stripped or all(character in TRIVIAL_CHARACTERS for character in stripped):
        return False
    return not stripped.startswith(COMMENT_PREFIXES) and stripped != "#"
