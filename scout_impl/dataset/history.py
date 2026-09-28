"""History features computed without lookahead.

Features for the commit at landing position i read only commits at positions below i, and
only events (reverts) that landed below i, so building the dataset up to commit i gives the
same values for it as building the whole history.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .categories import SZZ_CLASSES, Category

DAY = 86_400
YEAR = 365 * DAY
BURST_WINDOW = 7 * DAY


@dataclass(frozen=True)
class CommitFacts:
    """What the history, label and split stages need from one record."""

    index: int
    sha: str
    parent: str | None
    landed: int
    author_id: str
    is_merge: bool
    pr_number: int | None
    is_revert: bool
    reverts_sha: str | None
    reverts_pr: int | None
    fix_like: bool
    submodule_only: bool
    areas: tuple[str, ...]
    changes: tuple[tuple[str, str | None, str | None], ...]
    szz_targets: tuple[tuple[str, tuple[tuple[int, int], ...]], ...]
    szz_deleted_lines: int
    churn: int


def facts_from_record(index: int, record: Mapping[str, Any], category: Category, landed: int) -> CommitFacts:
    """``landed`` is the running maximum of committer times, so landing time never goes backwards."""
    commit = record["commit"]
    message = record["message"]
    areas = record["areas"]
    targets = []
    deleted = 0
    for item in record["files"]:
        if item["file_class"] not in SZZ_CLASSES or item["binary"] or not item["old_path"]:
            continue
        ranges = tuple(
            (old_start, old_start + old_count - 1) for old_start, old_count, _, _ in item["hunks"] if old_count > 0
        )
        if ranges:
            targets.append((item["old_path"], ranges))
            deleted += sum(end - start + 1 for start, end in ranges)
    return CommitFacts(
        index=index,
        sha=commit["sha"],
        parent=commit["parents"][0] if commit["parents"] else None,
        landed=landed,
        author_id=commit["author_id"],
        is_merge=commit["is_merge"],
        pr_number=message["pr_number"],
        is_revert=message["revert"]["is_revert"],
        reverts_sha=message["revert"]["reverts_sha"],
        reverts_pr=message["revert"]["reverts_pr"],
        fix_like=category.fix_like and not commit["is_merge"],
        submodule_only=record["features"]["is_submodule_only"],
        areas=tuple(
            [f"component:{item['id']}" for item in areas["components"]]
            + [f"feature:{item['id']}" for item in areas["features"]]
        ),
        changes=tuple((item["status"], item["old_path"], item["new_path"]) for item in record["files"]),
        szz_targets=tuple(targets),
        szz_deleted_lines=deleted,
        churn=record["features"]["churn"],
    )


@dataclass
class _FileHistory:
    commits: list[int] = field(default_factory=list)
    authors: set[str] = field(default_factory=set)
    fixes: list[int] = field(default_factory=list)
    last_epoch: int = 0


HISTORY_FEATURES = (
    "author_prior_commits",
    "author_area_experience",
    "author_recent_experience",
    "author_first_commit",
    "file_prior_changes",
    "file_prior_changes_max",
    "file_prior_authors",
    "file_prior_fix_touches",
    "file_days_since_last_change",
    "file_new_share",
    "area_prior_commits",
    "area_revert_rate",
    "area_commits_last_7d",
)


def history_features(facts: Sequence[CommitFacts], reverted_by: Mapping[int, int]) -> list[dict[str, Any]]:
    """One feature dict per commit. ``reverted_by`` maps a target index to its revert's index."""
    reverts_landing: dict[int, list[int]] = defaultdict(list)
    for target, revert in reverted_by.items():
        reverts_landing[revert].append(target)

    author_epochs: dict[str, list[int]] = defaultdict(list)
    author_areas: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    files: dict[str, _FileHistory] = {}
    area_epochs: dict[str, list[int]] = defaultdict(list)
    area_reverted: dict[str, int] = defaultdict(int)
    rows = []

    for fact in facts:
        epochs = author_epochs[fact.author_id]
        known = author_areas[fact.author_id]
        histories = [files[path] for path in _prior_paths(fact) if path in files]
        touched = len(fact.changes)
        union: set[int] = set()
        fixes: set[int] = set()
        authors: set[str] = set()
        for history in histories:
            union.update(history.commits)
            fixes.update(history.fixes)
            authors.update(history.authors)
        ages = [(fact.landed - history.last_epoch) / DAY for history in histories]
        area_prior = sum(len(area_epochs[area]) for area in fact.areas)
        rows.append(
            {
                "author_prior_commits": len(epochs),
                "author_area_experience": max((known[area] for area in fact.areas), default=0),
                "author_recent_experience": _recent_experience(epochs, fact.landed),
                "author_first_commit": not epochs,
                "file_prior_changes": len(union),
                "file_prior_changes_max": max((len(history.commits) for history in histories), default=0),
                "file_prior_authors": len(authors),
                "file_prior_fix_touches": len(fixes),
                "file_days_since_last_change": round(sum(ages) / len(ages), 3) if ages else None,
                "file_new_share": round(1 - len(histories) / touched, 6) if touched else 0.0,
                "area_prior_commits": area_prior,
                "area_revert_rate": round(sum(area_reverted[area] for area in fact.areas) / area_prior, 6)
                if area_prior
                else 0.0,
                "area_commits_last_7d": max(
                    (_count_since(area_epochs[area], fact.landed - BURST_WINDOW) for area in fact.areas), default=0
                ),
            }
        )

        epochs.append(fact.landed)
        for area in fact.areas:
            known[area] += 1
            area_epochs[area].append(fact.landed)
        _advance_files(files, fact)
        for target in reverts_landing.get(fact.index, ()):
            for area in facts[target].areas:
                area_reverted[area] += 1
    return rows


def _prior_paths(fact: CommitFacts) -> list[str]:
    """Paths whose earlier history this commit continues (the old side of renames)."""
    return sorted({old for status, old, _ in fact.changes if old and status != "A"})


def _advance_files(files: dict[str, _FileHistory], fact: CommitFacts) -> None:
    for status, old, new in fact.changes:
        if status == "R" and old and new:
            history = files.pop(old, None) or _FileHistory()
            files[new] = history
        elif status == "D" and old:
            history = files.pop(old, None) or _FileHistory()
        else:
            path = new or old
            assert path is not None
            history = files.setdefault(path, _FileHistory())
        history.commits.append(fact.index)
        history.authors.add(fact.author_id)
        history.last_epoch = fact.landed
        if fact.fix_like:
            history.fixes.append(fact.index)


def _recent_experience(epochs: list[int], now: int) -> float:
    """Kamei's REXP: prior commits weighted 1 / (1 + whole years ago)."""
    total = 0.0
    upper = len(epochs)
    years = 0
    while upper > 0:
        lower = bisect.bisect_right(epochs, now - (years + 1) * YEAR, 0, upper)
        total += (upper - lower) / (years + 1)
        upper = lower
        years += 1
    return round(total, 6)


def _count_since(epochs: list[int], since: int) -> int:
    return len(epochs) - bisect.bisect_left(epochs, since)
