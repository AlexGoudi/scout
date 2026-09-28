"""Time split: 70% train, 15% validation, 15% test by landing order.

A commit landing within ``GAP_DAYS`` before a boundary, or before the snapshot, goes to
``gap``: its label window would straddle the boundary, or would not have closed yet.
Labels are recomputed per split so that each counts only events that landed before the
split ends, which is what a model trained at that moment could have known.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .history import DAY, CommitFacts
from .labels import REVERT_WINDOWS_DAYS

FRACTIONS = (0.70, 0.15, 0.15)
GAP_DAYS = 90
SPLITS = ("train", "validation", "test")
LABELS = ("bug_introducing", "reverted", *(f"reverted_within_{days}d" for days in REVERT_WINDOWS_DAYS))


@dataclass(frozen=True)
class Split:
    assignment: tuple[str, ...]
    ends: Mapping[str, int]
    boundaries: Mapping[str, int]
    exclude_matched: int
    exclude_unmatched: tuple[str, ...]


def read_exclusions(path: str | Path | None) -> list[str]:
    if path is None:
        return []
    entries = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        entry = line.split("#", 1)[0].strip().lower()
        if entry:
            entries.append(entry)
    return entries


def assign_splits(facts: Sequence[CommitFacts], exclude: Sequence[str] = ()) -> Split:
    """Split by landing order; ``exclude`` holds SHAs or unique prefixes that go to ``holdout``."""
    held, unmatched = _resolve(facts, exclude)
    if not facts:
        return Split((), {}, {}, 0, tuple(unmatched))
    snapshot = facts[-1].landed
    gap = GAP_DAYS * DAY
    eligible = [fact for fact in facts if fact.landed <= snapshot - gap and fact.sha not in held]
    count = len(eligible)
    first_validation = int(count * FRACTIONS[0])
    first_test = int(count * (FRACTIONS[0] + FRACTIONS[1]))
    validation_start = eligible[first_validation].landed if first_validation < count else snapshot
    test_start = eligible[first_test].landed if first_test < count else snapshot
    ends = {"train": validation_start, "validation": test_start, "test": snapshot}

    assignment = []
    for fact in facts:
        if fact.sha in held:
            assignment.append("holdout")
        elif fact.landed > snapshot - gap:
            assignment.append("gap")
        elif fact.landed < validation_start:
            assignment.append("train" if fact.landed <= validation_start - gap else "gap")
        elif fact.landed < test_start:
            assignment.append("validation" if fact.landed <= test_start - gap else "gap")
        else:
            assignment.append("test")
    return Split(
        assignment=tuple(assignment),
        ends=ends,
        boundaries={"validation_start": validation_start, "test_start": test_start, "snapshot": snapshot},
        exclude_matched=len(held),
        exclude_unmatched=tuple(unmatched),
    )


def split_labels(labels: Mapping[str, Any], split: str, ends: Mapping[str, int]) -> dict[str, Any]:
    """Labels as known when ``split`` ends; outside train, validation and test they stay as-of-snapshot."""
    end = ends.get(split)
    reverted_seen = labels["reverted"]
    fixed_seen = labels["bug_introducing"]
    if end is not None:
        if reverted_seen:
            reverted_seen = labels["reverted_landed"] <= end
        if fixed_seen:
            fixed_seen = labels["fixed_landed"] <= end
    result = {"bug_introducing": fixed_seen, "reverted": reverted_seen}
    for days in REVERT_WINDOWS_DAYS:
        value = labels[f"reverted_within_{days}d"]
        result[f"reverted_within_{days}d"] = value if not value else bool(reverted_seen)
    return result


def _resolve(facts: Sequence[CommitFacts], exclude: Sequence[str]) -> tuple[set[str], list[str]]:
    shas = sorted(fact.sha for fact in facts)
    held: set[str] = set()
    unmatched = []
    for entry in exclude:
        matches = [sha for sha in shas if sha.startswith(entry)] if len(entry) >= 7 else []
        if len(matches) > 1:
            raise ValueError(f"excluded commit {entry!r} is ambiguous in this history")
        if matches:
            held.add(matches[0])
        else:
            unmatched.append(entry)
    return held, sorted(set(unmatched))
