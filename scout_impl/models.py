"""Serializable change-set model shared by the Scout stages.

The objects here are the contract between ingest (stage 0) and everything downstream:
the prefilter reads `path_class` and the per-commit file lists, the detectors read the
diff hunks, and the citation resolver maps a claim back to a file and line through the
`old_lineno` / `new_lineno` carried on every diff line. Every object round-trips
through `to_dict` / `from_dict` so a change set can be cached on disk and replayed by
the backtest harness without re-running git.

Nothing here knows which repository it is describing. Path classes come from a
`RepoAdapter` (`scout_impl/repos/`), and both `ChangeSet.repo` and the `repo:class_id`
form each `FileDiff` serializes carry the adapter identity needed to read the change set
back — a cached change set is interpretable without being told where it came from.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .repos import PathClass, RepoAdapter, get_adapter, path_class_from_qualified_id

SCHEMA_VERSION = "1.0"

MODE_RANGE = "range"
MODE_SYNC = "sync"

CHANGE_ADDED = "added"
CHANGE_DELETED = "deleted"
CHANGE_MODIFIED = "modified"
CHANGE_RENAMED = "renamed"
CHANGE_COPIED = "copied"

LINE_CONTEXT = " "
LINE_ADDED = "+"
LINE_REMOVED = "-"


@dataclass(frozen=True)
class DiffLine:
    """One line of a hunk, carrying the line numbers a citation resolves against."""

    kind: str
    content: str
    old_lineno: Optional[int] = None
    new_lineno: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "content": self.content,
            "old_lineno": self.old_lineno,
            "new_lineno": self.new_lineno,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "DiffLine":
        return cls(
            kind=str(payload["kind"]),
            content=str(payload["content"]),
            old_lineno=payload.get("old_lineno"),
            new_lineno=payload.get("new_lineno"),
        )


@dataclass(frozen=True)
class DiffHunk:
    """A single `@@` hunk with its original header ranges."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    section: str = ""
    lines: List[DiffLine] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "old_start": self.old_start,
            "old_count": self.old_count,
            "new_start": self.new_start,
            "new_count": self.new_count,
            "section": self.section,
            "lines": [line.to_dict() for line in self.lines],
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "DiffHunk":
        return cls(
            old_start=int(payload["old_start"]),
            old_count=int(payload["old_count"]),
            new_start=int(payload["new_start"]),
            new_count=int(payload["new_count"]),
            section=str(payload.get("section") or ""),
            lines=[DiffLine.from_dict(item) for item in payload.get("lines") or []],
        )


@dataclass(frozen=True)
class FileDiff:
    """Per-file diff for one commit, classified for the prefilter."""

    path: str
    change_type: str
    path_class: PathClass
    old_path: Optional[str] = None
    is_binary: bool = False
    similarity: Optional[int] = None
    hunks: List[DiffHunk] = field(default_factory=list)

    @property
    def added_lines(self) -> List[DiffLine]:
        return [line for hunk in self.hunks for line in hunk.lines if line.kind == LINE_ADDED]

    @property
    def removed_lines(self) -> List[DiffLine]:
        return [line for hunk in self.hunks for line in hunk.lines if line.kind == LINE_REMOVED]

    @property
    def additions(self) -> int:
        return len(self.added_lines)

    @property
    def deletions(self) -> int:
        return len(self.removed_lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "change_type": self.change_type,
            "path_class": self.path_class.qualified_id,
            "old_path": self.old_path,
            "is_binary": self.is_binary,
            "similarity": self.similarity,
            "hunks": [hunk.to_dict() for hunk in self.hunks],
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "FileDiff":
        return cls(
            path=str(payload["path"]),
            change_type=str(payload["change_type"]),
            path_class=path_class_from_qualified_id(payload["path_class"]),
            old_path=payload.get("old_path"),
            is_binary=bool(payload.get("is_binary")),
            similarity=payload.get("similarity"),
            hunks=[DiffHunk.from_dict(item) for item in payload.get("hunks") or []],
        )


@dataclass(frozen=True)
class CommitInfo:
    """One commit of the change set together with its first-parent diff."""

    sha: str
    parents: List[str] = field(default_factory=list)
    author_name: str = ""
    author_email: str = ""
    authored_date: str = ""
    committed_date: str = ""
    subject: str = ""
    body: str = ""
    files: List[FileDiff] = field(default_factory=list)

    @property
    def short_sha(self) -> str:
        return self.sha[:9]

    @property
    def changed_paths(self) -> List[str]:
        return [file_diff.path for file_diff in self.files]

    @property
    def path_classes(self) -> List[PathClass]:
        return sorted({file_diff.path_class for file_diff in self.files}, key=lambda item: item.rank)

    @property
    def highest_path_class(self) -> Optional[PathClass]:
        classes = self.path_classes
        return classes[0] if classes else None

    @property
    def additions(self) -> int:
        return sum(file_diff.additions for file_diff in self.files)

    @property
    def deletions(self) -> int:
        return sum(file_diff.deletions for file_diff in self.files)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sha": self.sha,
            "parents": list(self.parents),
            "author_name": self.author_name,
            "author_email": self.author_email,
            "authored_date": self.authored_date,
            "committed_date": self.committed_date,
            "subject": self.subject,
            "body": self.body,
            "files": [file_diff.to_dict() for file_diff in self.files],
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "CommitInfo":
        return cls(
            sha=str(payload["sha"]),
            parents=[str(item) for item in payload.get("parents") or []],
            author_name=str(payload.get("author_name") or ""),
            author_email=str(payload.get("author_email") or ""),
            authored_date=str(payload.get("authored_date") or ""),
            committed_date=str(payload.get("committed_date") or ""),
            subject=str(payload.get("subject") or ""),
            body=str(payload.get("body") or ""),
            files=[FileDiff.from_dict(item) for item in payload.get("files") or []],
        )


@dataclass(frozen=True)
class LocalPatch:
    """A fork-local commit that is not upstream; the input to detector D3.

    `overlapping_paths` is the intersection with the incoming change set, which is where
    conflict resolution can silently drop a local fix.
    """

    sha: str
    subject: str = ""
    authored_date: str = ""
    paths: List[str] = field(default_factory=list)
    overlapping_paths: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sha": self.sha,
            "subject": self.subject,
            "authored_date": self.authored_date,
            "paths": list(self.paths),
            "overlapping_paths": list(self.overlapping_paths),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "LocalPatch":
        return cls(
            sha=str(payload["sha"]),
            subject=str(payload.get("subject") or ""),
            authored_date=str(payload.get("authored_date") or ""),
            paths=[str(item) for item in payload.get("paths") or []],
            overlapping_paths=[str(item) for item in payload.get("overlapping_paths") or []],
        )


@dataclass(frozen=True)
class ChangeSetSpec:
    """What to ingest. `base_ref` is the fork ref in sync mode, the range base otherwise."""

    base_ref: str
    head_ref: str
    mode: str = MODE_RANGE
    merge_base: bool = False
    include_merges: bool = False
    context_lines: int = 3

    def to_dict(self) -> Dict[str, Any]:
        return {
            "base_ref": self.base_ref,
            "head_ref": self.head_ref,
            "mode": self.mode,
            "merge_base": self.merge_base,
            "include_merges": self.include_merges,
            "context_lines": self.context_lines,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ChangeSetSpec":
        return cls(
            base_ref=str(payload["base_ref"]),
            head_ref=str(payload["head_ref"]),
            mode=str(payload.get("mode") or MODE_RANGE),
            merge_base=bool(payload.get("merge_base")),
            include_merges=bool(payload.get("include_merges")),
            context_lines=int(payload.get("context_lines", 3)),
        )

    @classmethod
    def from_range(cls, range_expr: str, **kwargs: Any) -> "ChangeSetSpec":
        """Parse a git range expression. `A...B` resolves the base to the merge base."""
        if "..." in range_expr:
            base_ref, _, head_ref = range_expr.partition("...")
            merge_base = True
        elif ".." in range_expr:
            base_ref, _, head_ref = range_expr.partition("..")
            merge_base = False
        else:
            raise ValueError(f"Not a commit range: {range_expr!r}; expected BASE..HEAD or BASE...HEAD")

        if not base_ref.strip():
            raise ValueError(f"Commit range {range_expr!r} is missing a base revision")

        return cls(
            base_ref=base_ref.strip(),
            head_ref=head_ref.strip() or "HEAD",
            merge_base=merge_base,
            **kwargs,
        )


@dataclass(frozen=True)
class ChangeSet:
    """Normalized change set produced by ingest."""

    base_sha: str
    head_sha: str
    spec: ChangeSetSpec
    repo: str
    commits: List[CommitInfo] = field(default_factory=list)
    local_patches: List[LocalPatch] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION

    @property
    def mode(self) -> str:
        return self.spec.mode

    @property
    def adapter(self) -> RepoAdapter:
        """The adapter whose rules classified this change set's paths."""
        return get_adapter(self.repo)

    @property
    def commit_count(self) -> int:
        return len(self.commits)

    @property
    def changed_paths(self) -> List[str]:
        """Union of paths touched by the change set, in stable order."""
        seen: Dict[str, None] = {}
        for commit in self.commits:
            for file_diff in commit.files:
                seen.setdefault(file_diff.path, None)
                if file_diff.old_path:
                    seen.setdefault(file_diff.old_path, None)
        return sorted(seen)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "repo": self.repo,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "spec": self.spec.to_dict(),
            "commits": [commit.to_dict() for commit in self.commits],
            "local_patches": [patch.to_dict() for patch in self.local_patches],
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ChangeSet":
        return cls(
            base_sha=str(payload["base_sha"]),
            head_sha=str(payload["head_sha"]),
            spec=ChangeSetSpec.from_dict(payload["spec"]),
            repo=str(payload["repo"]),
            commits=[CommitInfo.from_dict(item) for item in payload.get("commits") or []],
            local_patches=[LocalPatch.from_dict(item) for item in payload.get("local_patches") or []],
            schema_version=str(payload.get("schema_version") or SCHEMA_VERSION),
        )
