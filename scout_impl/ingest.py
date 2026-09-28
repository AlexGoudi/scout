"""Stage 0 ingest: resolve a commit range or a fork sync delta into a `ChangeSet`.

Implements FR-1 for the two surfaces that exist in this window: the local range used by
the developer CLI, and the sync delta used by the fork gate. Replay is covered by
`ChangeSet.from_dict`, so a cached change set needs no git at all.

Ingest takes a `RepoSource`, not a directory, so the same code path serves a working
copy on disk and a pull request fetched from a remote nobody has cloned. It takes a
`RepoAdapter` too, and identifies one from the tree when the caller does not name it.
"""

import logging
from typing import List, Optional, Set

from .diffparse import parse_diff
from .gitcmd import EMPTY_TREE_SHA
from .models import (
    ChangeSet,
    ChangeSetSpec,
    CommitInfo,
    LocalPatch,
    MODE_SYNC,
)
from .repos import RepoAdapter
from .source import RepoSource, as_source

logger = logging.getLogger(__name__)

_RECORD_SEPARATOR = "\x1e"
_FIELD_SEPARATOR = "\x1f"
_LOG_FORMAT = _FIELD_SEPARATOR.join(["%H", "%P", "%an", "%ae", "%aI", "%cI", "%s", "%B"])
_PATCH_LOG_FORMAT = _FIELD_SEPARATOR.join(["%H", "%aI", "%s"]) + _FIELD_SEPARATOR


def resolve(
    spec: ChangeSetSpec,
    source: object,
    adapter: Optional[RepoAdapter] = None,
) -> ChangeSet:
    """Resolve `spec` against `source`, a `RepoSource` or a path to a working copy.

    `adapter` names the repository's classification rules; without one, Scout identifies
    the repository from the tree at the head commit, which costs no file content.
    """
    repo = as_source(source)

    head_sha = repo.rev_parse(spec.head_ref)
    if spec.merge_base or spec.mode == MODE_SYNC:
        base_sha = repo.merge_base(spec.base_ref, spec.head_ref)
    else:
        base_sha = repo.rev_parse(spec.base_ref)

    adapter = adapter or repo.detect_adapter(head_sha)
    commits = _load_commits(repo, base_sha, head_sha, spec, adapter)
    logger.info(
        "Ingested %d commit(s) for %s..%s in %s mode from %s as %s",
        len(commits),
        base_sha[:9],
        head_sha[:9],
        spec.mode,
        repo.describe,
        adapter.name,
    )

    local_patches: List[LocalPatch] = []
    if spec.mode == MODE_SYNC:
        changed = {path for commit in commits for path in commit.changed_paths}
        local_patches = _load_local_patches(repo, head_sha, repo.rev_parse(spec.base_ref), changed)
        logger.info(
            "Sync mode: %d local patch commit(s), %d touching incoming paths",
            len(local_patches),
            sum(1 for patch in local_patches if patch.overlapping_paths),
        )

    return ChangeSet(
        base_sha=base_sha,
        head_sha=head_sha,
        spec=spec,
        repo=adapter.name,
        commits=commits,
        local_patches=local_patches,
    )


def _load_commits(
    repo: RepoSource,
    base_sha: str,
    head_sha: str,
    spec: ChangeSetSpec,
    adapter: RepoAdapter,
) -> List[CommitInfo]:
    log_args = ["log", "-z", "--reverse", f"--format={_LOG_FORMAT}"]
    if not spec.include_merges:
        log_args.append("--no-merges")
    log_args.append(f"{base_sha}..{head_sha}")

    commits: List[CommitInfo] = []
    for record in repo.git(*log_args).split("\0"):
        if not record.strip():
            continue
        commits.append(_build_commit(repo, record, spec.context_lines, adapter))
    return commits


def _build_commit(repo: RepoSource, record: str, context_lines: int, adapter: RepoAdapter) -> CommitInfo:
    # The body is last so that a separator byte inside a commit message cannot shift fields.
    fields = record.split(_FIELD_SEPARATOR, 7)
    sha, parents_raw, author_name, author_email, authored_date, committed_date, subject, body = fields
    parents = parents_raw.split()

    diff_text = repo.git(
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--find-renames",
        f"--unified={context_lines}",
        parents[0] if parents else EMPTY_TREE_SHA,
        sha,
    )

    return CommitInfo(
        sha=sha,
        parents=parents,
        author_name=author_name,
        author_email=author_email,
        authored_date=authored_date,
        committed_date=committed_date,
        subject=subject,
        body=body.strip(),
        files=parse_diff(diff_text, adapter),
    )


def _load_local_patches(
    repo: RepoSource,
    upstream_ref: str,
    local_ref: str,
    incoming_paths: Set[str],
) -> List[LocalPatch]:
    """Commits on the fork but not upstream, annotated with their overlap with the delta."""
    output = repo.git(
        "log",
        "--reverse",
        "--no-merges",
        "--name-only",
        f"--format={_RECORD_SEPARATOR}{_PATCH_LOG_FORMAT}",
        f"{upstream_ref}..{local_ref}",
    )

    patches: List[LocalPatch] = []
    for record in output.split(_RECORD_SEPARATOR):
        if not record.strip():
            continue
        sha, authored_date, subject, paths_blob = record.split(_FIELD_SEPARATOR, 3)
        paths = [line for line in paths_blob.splitlines() if line.strip()]
        patches.append(
            LocalPatch(
                sha=sha,
                subject=subject,
                authored_date=authored_date,
                paths=paths,
                overlapping_paths=sorted(set(paths) & incoming_paths),
            )
        )
    return patches
