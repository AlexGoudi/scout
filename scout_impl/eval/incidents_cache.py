"""Cached incident mining under the adapter calibration directory."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from ..gitcmd import GitRepo
from ..incidents import DEFAULT_GREP, Incident, mine_incidents, read_corpus, write_corpus
from ._cache import CODE_VERSION, Step, fingerprint
logger = logging.getLogger(__name__)

INCIDENTS_FILE = "incidents.jsonl"
STEP_NAME = "incidents"


def incidents_fingerprint(grep: str) -> str:
    return fingerprint(CODE_VERSION, "incidents", grep)


def load_cached_incidents(directory: Path) -> list[Incident]:
    path = directory / INCIDENTS_FILE
    if not path.is_file():
        return []
    return read_corpus(path)


def run_incidents_cache(
    repo: GitRepo,
    directory: Path,
    revision: str = "HEAD",
    grep: str = DEFAULT_GREP,
    limit: Optional[int] = None,
    refresh: bool = False,
) -> list[Incident]:
    """Mine incidents incrementally; store JSONL in ``directory``."""
    if limit is not None:
        incidents = mine_incidents(repo, revision=revision, grep=grep, limit=limit)
        write_corpus(incidents, directory / INCIDENTS_FILE)
        Step(directory, STEP_NAME, incidents_fingerprint(grep), refresh=refresh).commit(
            [INCIDENTS_FILE],
            {"tip": repo.rev_parse(revision), "grep": grep, "limit": limit},
        )
        return incidents

    tip = repo.rev_parse(revision)
    step = Step(directory, STEP_NAME, incidents_fingerprint(grep), refresh=refresh)
    if step.fresh() and (directory / INCIDENTS_FILE).is_file():
        cursor = step.cursor or {}
        if cursor.get("tip") == tip:
            logger.info("incidents: cache fresh at %s (0 to process)", tip[:12])
            return load_cached_incidents(directory)

    prev_tip = (step.cursor or {}).get("tip") if not refresh else None
    existing = load_cached_incidents(directory) if prev_tip and not refresh else []
    by_sha = {item.revert_sha: item for item in existing}

    if refresh or not prev_tip or not repo.is_ancestor(prev_tip, tip):
        if prev_tip and not refresh and not repo.is_ancestor(prev_tip, tip):
            logger.warning("incidents: %s is not ancestor of %s; full re-mine", prev_tip[:12], tip[:12])
        incidents = mine_incidents(repo, revision=revision, grep=grep)
        write_corpus(incidents, directory / INCIDENTS_FILE)
        step.commit([INCIDENTS_FILE], {"tip": tip, "grep": grep})
        return incidents

    range_spec = f"{prev_tip}..{tip}"
    new_incidents = mine_incidents(repo, revision=tip, grep=grep, range_spec=range_spec)
    logger.info("incidents: %d revert(s) to process in %s..%s", len(new_incidents), prev_tip[:12], tip[:12])
    if new_incidents:
        for item in new_incidents:
            by_sha[item.revert_sha] = item
        merged = sorted(by_sha.values(), key=lambda item: item.revert_committed_date, reverse=True)
        write_corpus(merged, directory / INCIDENTS_FILE)
    step.commit([INCIDENTS_FILE], {"tip": tip, "grep": grep})
    return load_cached_incidents(directory)
