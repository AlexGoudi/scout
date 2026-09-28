"""Assemble commit records and serialize them deterministically.

Single-commit mining and the dataset stream share ``extract_records``: one ``cat-file``
process for commit objects, one ``git log --raw --numstat`` process for the changed paths
and one ``git log --patch`` process for hunks, over however many commits are asked for.
"""

from __future__ import annotations

import dataclasses
import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Iterator, Mapping, Sequence

from . import EXTRACTOR_VERSION
from .diff import LINE_KINDS, FilePatch, Key, PatchLimits, iter_commit_patches, merge_patches
from .gitio import EMPTY_TREE, CommitObject, Git, GitError, RawChange
from .message import DEFAULT_AUTHOR_SALT, Message, Redactor, author_id, is_bot, parse_message
from .taxonomy import ENTITY_KINDS, FILE_CLASSES, Areas, Taxonomy, classify, file_class, load_taxonomy

SCHEMA_ID = "scout-commit"
SCHEMA_VERSION = "1.1"
PATCH_MAX_BYTES_PER_COMMIT = 200_000
UNMAPPED_LIST_MAX = 100
SCOPES_PER_FILE = 20
SCOPE_MAX_CHARS = 160


@dataclass(frozen=True)
class Provenance:
    extractor_version: str
    taxonomy_sha: str
    redaction_sha: str
    repo: str | None


@dataclass(frozen=True)
class CommitMeta:
    sha: str
    tree: str
    parents: tuple[str, ...]
    is_merge: bool
    is_root: bool
    authored_at: str
    committed_at: str
    committed_epoch: int
    author_id: str
    author_is_bot: bool
    base: str
    base_strategy: str


@dataclass(frozen=True)
class Revert:
    is_revert: bool
    depth: int
    is_nested: bool
    reverts_sha: str | None
    reverts_pr: int | None


@dataclass(frozen=True)
class MessageRecord:
    subject: str
    body: str
    body_truncated: bool
    body_length: int
    pr_number: int | None
    subject_tags: tuple[str, ...]
    sections: Mapping[str, str | None]
    trailer_counts: Mapping[str, int]
    revert: Revert
    pulled_commit_lines: int


@dataclass(frozen=True)
class FileRecord:
    status: str
    old_path: str | None
    new_path: str | None
    old_mode: str | None
    new_mode: str | None
    old_blob: str | None
    new_blob: str | None
    similarity: int | None
    additions: int | None
    deletions: int | None
    binary: bool
    file_class: str
    hunks: tuple[tuple[int, int, int, int], ...]
    patch: str | None
    patch_truncated: bool
    scopes: tuple[str, ...]
    line_kinds: Mapping[str, tuple[int, int]]

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""


@dataclass(frozen=True)
class Submodule:
    path: str
    name: str
    url: str | None
    old_sha: str | None
    new_sha: str | None


@dataclass(frozen=True)
class CommitRecord:
    schema: str
    schema_version: str
    provenance: Provenance
    commit: CommitMeta
    message: MessageRecord
    files: tuple[FileRecord, ...]
    submodules: tuple[Submodule, ...]
    areas: Areas
    features: Mapping[str, Any]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class Context:
    """Everything a record depends on besides the commit itself."""

    taxonomy: Taxonomy
    redactor: Redactor
    salt: str
    repo: str | None
    limits: PatchLimits = PatchLimits()


def make_context(
    git: Git,
    names_revision: str,
    *,
    taxonomy: Taxonomy | None = None,
    salt: str = DEFAULT_AUTHOR_SALT,
    limits: PatchLimits = PatchLimits(),
) -> Context:
    """The redaction universe is every author and committer name reachable from ``names_revision``."""
    taxonomy = taxonomy or load_taxonomy()
    redactor = Redactor(git.identity_names(names_revision), taxonomy.name_stoplist)
    return Context(taxonomy=taxonomy, redactor=redactor, salt=salt, repo=git.remote_url(), limits=limits)


def mine_commit(git: Git, revision: str, context: Context) -> CommitRecord:
    sha = git.resolve_commit(revision)
    return next(iter(extract_records(git, [sha], context)))


def extract_records(git: Git, shas: Sequence[str], context: Context) -> Iterator[CommitRecord]:
    """Records for ``shas`` in the order given; reads Git objects only."""
    if not shas:
        return
    commits = {commit.sha: commit for commit in git.read_commits(shas)}
    parents = sorted({commit.parents[0] for commit in commits.values() if commit.parents})
    missing = git.missing_objects(parents)
    if missing:
        raise GitError(f"{len(missing)} first parents are missing from the clone; is it shallow?")
    changes = git.read_changes(shas)
    produced = set()
    with git.patch_lines(shas) as lines:
        for sha, patches in iter_commit_patches(lines, context.limits):
            if sha not in commits or sha in produced:
                raise GitError(f"unexpected commit {sha} in the patch stream")
            produced.add(sha)
            yield build_record(git, commits[sha], changes.get(sha, ()), patches, context)
    absent = [sha for sha in shas if sha not in produced]
    if absent:
        raise GitError(f"the patch stream omitted {len(absent)} commits, starting with {absent[0]}")


def build_record(
    git: Git,
    commit: CommitObject,
    changes: Sequence[RawChange],
    patches: Mapping[Key, list[FilePatch]],
    context: Context,
) -> CommitRecord:
    taxonomy = context.taxonomy
    redact = context.redactor.with_names((commit.author_name, commit.committer_name))
    gitlinks = [change for change in changes if change.is_gitlink]
    message = parse_message(commit.message, redact=redact, count_pulled=bool(gitlinks))

    modules: dict[str, tuple[str, str | None]] = {}
    if gitlinks:
        modules = git.gitmodules(commit.sha)
        if commit.parents and any(change.path not in modules for change in gitlinks):
            for path, value in git.gitmodules(commit.parents[0]).items():
                modules.setdefault(path, value)
    submodules = []
    for change in gitlinks:
        name, url = modules.get(change.path, (change.path, None))
        submodules.append(
            Submodule(
                path=change.path,
                name=name,
                url=redact(url) if url else None,
                old_sha=change.old_blob,
                new_sha=change.new_blob,
            )
        )

    warnings: list[str] = []
    files = _files(changes, patches, taxonomy, redact, warnings)
    paths = {path for change in changes for path in (change.old_path, change.new_path) if path}
    areas = classify(paths, {module.path: module.name for module in submodules}, taxonomy)
    features = intrinsic_features(files, areas, message)
    if len(areas.unmapped_paths) > UNMAPPED_LIST_MAX:
        areas = dataclasses.replace(areas, unmapped_paths=areas.unmapped_paths[:UNMAPPED_LIST_MAX])
        warnings.append(f"unmapped_paths capped at {UNMAPPED_LIST_MAX}")

    reverts_sha = message.reverts_sha
    if reverts_sha is not None:
        reverts_sha = git.resolve_optional(reverts_sha) or reverts_sha

    return CommitRecord(
        schema=SCHEMA_ID,
        schema_version=SCHEMA_VERSION,
        provenance=Provenance(
            extractor_version=EXTRACTOR_VERSION,
            taxonomy_sha=taxonomy.sha256,
            redaction_sha=context.redactor.digest,
            repo=context.repo,
        ),
        commit=CommitMeta(
            sha=commit.sha,
            tree=commit.tree,
            parents=commit.parents,
            is_merge=len(commit.parents) > 1,
            is_root=not commit.parents,
            authored_at=utc(commit.authored_epoch),
            committed_at=utc(commit.committed_epoch),
            committed_epoch=commit.committed_epoch,
            author_id=author_id(commit.author_email, context.salt),
            author_is_bot=is_bot(commit.author_name, commit.author_email, taxonomy.bots),
            base=commit.parents[0] if commit.parents else EMPTY_TREE,
            base_strategy="first-parent" if commit.parents else "empty-tree",
        ),
        message=MessageRecord(
            subject=message.subject,
            body=message.body,
            body_truncated=message.body_truncated,
            body_length=message.body_length,
            pr_number=message.pr_number,
            subject_tags=message.subject_tags,
            sections=message.sections,
            trailer_counts=message.trailer_counts,
            revert=Revert(
                is_revert=message.revert_depth > 0,
                depth=message.revert_depth,
                is_nested=message.revert_depth > 1,
                reverts_sha=reverts_sha,
                reverts_pr=message.reverts_pr,
            ),
            pulled_commit_lines=message.pulled_commit_lines,
        ),
        files=files,
        submodules=tuple(submodules),
        areas=areas,
        features=features,
        warnings=tuple(sorted(set(warnings))),
    )


def intrinsic_features(files: Sequence[FileRecord], areas: Areas, message: Message) -> dict[str, Any]:
    """Size, diffusion, content mix and message statistics known when the commit is made."""
    total = len(files)
    classes = Counter(item.file_class for item in files)
    statuses = Counter(item.status for item in files)
    text_files = [item for item in files if not item.binary]
    additions = sum(item.additions or 0 for item in text_files)
    deletions = sum(item.deletions or 0 for item in text_files)
    weights = [(item.additions or 0) + (item.deletions or 0) for item in text_files]
    entropy = _entropy(weights)
    kind_lines = {kind: sum(sum(item.line_kinds[kind]) for item in text_files) for kind in LINE_KINDS}
    logic_churn = additions + deletions - sum(kind_lines.values())
    parts = [PurePosixPath(item.path).parts for item in files]
    directories = {"/".join(part[:-1]) or "<root>" for part in parts}
    subsystems = {part[0] if len(part) > 1 else "<root>" for part in parts}
    kinds = Counter(entity.kind for entity in areas.entities)
    features: dict[str, Any] = {
        "file_count": total,
        "additions": additions,
        "deletions": deletions,
        "churn": additions + deletions,
        "logic_churn": logic_churn,
        "blank_line_count": kind_lines["blank"],
        "whitespace_only_line_count": kind_lines["whitespace_only"],
        "comment_line_count": kind_lines["comment"],
        "is_comment_or_whitespace_only": additions + deletions > 0 and logic_churn == 0
        and len(text_files) == total and classes["submodule"] == 0,
        "scope_count": sum(len(item.scopes) for item in files),
        "directory_count": len(directories),
        "subsystem_count": len(subsystems),
        "entropy": entropy,
        "entropy_normalized": round(entropy / math.log2(total), 6) if total > 1 else 0.0,
        "max_path_depth": max((len(part) for part in parts), default=0),
        "added_file_count": statuses["A"],
        "deleted_file_count": statuses["D"],
        "modified_file_count": statuses["M"] + statuses["T"],
        "renamed_file_count": statuses["R"] + statuses["C"],
        "is_doc_only": total > 0 and classes["doc"] == total,
        "is_test_only": total > 0 and classes["test"] == total,
        "is_submodule_only": total > 0 and classes["submodule"] == total,
        "touches_tests": classes["test"] > 0,
        "component_count": len(areas.components),
        "feature_area_count": len(areas.features),
        "entity_count": len(areas.entities),
        "unmapped_path_count": len(areas.unmapped_paths),
        "subject_length": len(message.subject),
        "body_length": message.body_length,
        "subject_tag_count": len(message.subject_tags),
        "has_pr_number": message.pr_number is not None,
        "has_why": message.sections.get("why") is not None,
        "has_how": message.sections.get("how") is not None,
        "has_verify": message.sections.get("verify") is not None,
        "pulled_commit_lines": message.pulled_commit_lines,
        "co_author_count": message.trailer_counts.get("co_authored_by", 0),
    }
    features.update({f"{name}_file_count": classes[name] for name in FILE_CLASSES})
    features.update({f"{kind}_count": kinds[kind] for kind in ENTITY_KINDS})
    return features


def record_to_dict(record: CommitRecord) -> dict[str, Any]:
    value = _normalize(record)
    assert isinstance(value, dict)
    return value


def record_to_json(record: CommitRecord | Mapping[str, Any], *, pretty: bool = False) -> str:
    """Stable JSON: sorted keys, no wall-clock values, valid UTF-8."""
    data = record_to_dict(record) if isinstance(record, CommitRecord) else record
    if pretty:
        return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def utc(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _files(
    changes: Sequence[RawChange],
    patches: Mapping[Key, list[FilePatch]],
    taxonomy: Taxonomy,
    redact: Redactor,
    warnings: list[str],
) -> tuple[FileRecord, ...]:
    budget = PATCH_MAX_BYTES_PER_COMMIT
    files = []
    for change in changes:
        patch = _lookup_patch(change, patches)
        if patch is None and not change.binary:
            warnings.append(f"{change.path}: no patch section in the commit diff")
        text = None
        truncated = False
        if patch is not None and patch.lines and not change.binary:
            kept = []
            truncated = patch.truncated
            for raw_line in patch.lines:
                line = redact(raw_line.decode("utf-8", "replace"))
                size = len(line.encode("utf-8")) + 1
                if size > budget:
                    truncated = True
                    break
                kept.append(line)
                budget -= size
            text = "".join(line + "\n" for line in kept)
        files.append(
            FileRecord(
                status=change.status,
                old_path=change.old_path,
                new_path=change.new_path,
                old_mode=change.old_mode,
                new_mode=change.new_mode,
                old_blob=change.old_blob,
                new_blob=change.new_blob,
                similarity=change.similarity,
                additions=change.additions,
                deletions=change.deletions,
                binary=change.binary,
                file_class=file_class(change.path, binary=change.binary, gitlink=change.is_gitlink, taxonomy=taxonomy),
                hunks=tuple(patch.hunks) if patch is not None else (),
                patch=text,
                patch_truncated=truncated,
                scopes=_scopes(patch, redact) if patch is not None and not change.binary else (),
                line_kinds={
                    kind: tuple(patch.line_kinds[kind]) if patch is not None and not change.binary else (0, 0)
                    for kind in LINE_KINDS
                },
            )
        )
    return tuple(files)


def _scopes(patch: FilePatch, redact: Redactor) -> tuple[str, ...]:
    """Distinct hunk scopes in hunk order, redacted before they are capped."""
    scopes: list[str] = []
    for scope in patch.scopes:
        text = redact(scope)[:SCOPE_MAX_CHARS].rstrip()
        if text and text not in scopes:
            scopes.append(text)
            if len(scopes) == SCOPES_PER_FILE:
                break
    return tuple(scopes)


def _lookup_patch(change: RawChange, patches: Mapping[Key, list[FilePatch]]) -> FilePatch | None:
    found = patches.get(change.key)
    if found is None and change.status == "T":
        found = [*patches.get((change.old_path, None), []), *patches.get((None, change.new_path), [])] or None
    return merge_patches(found) if found else None


def _entropy(weights: Sequence[int]) -> float:
    total = sum(weights)
    if total <= 0:
        return 0.0
    value = -sum((weight / total) * math.log2(weight / total) for weight in weights if weight > 0)
    return round(value, 6) + 0.0


def _normalize(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {item.name: _normalize(getattr(value, item.name)) for item in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _normalize(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, str):
        return _valid_text(value)
    return value


def _valid_text(text: str) -> str:
    """Paths are decoded with surrogateescape; the JSON output must be valid UTF-8."""
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
