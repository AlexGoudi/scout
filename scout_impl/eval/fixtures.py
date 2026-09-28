"""Pinned per-item fixtures: the cause's tree and the cause's own change, captured once (NFR-10).

One file per corpus item, holding exactly what the static stage needs to analyze the change
`parent..cause` offline: a `TreeFixture` of the tree at `cause_sha`, filtered the way the
conformance fixtures are, the `ChangeSet` of that range, and the capture's own measurements.

**No lookahead, by construction and then by check.** Capture fetches `cause_sha` alone, at
`--depth=2 --filter=blob:none`, into a cache directory keyed by item and cause that nothing
else ever fetches into. The object store therefore holds the cause, its parents and their
trees, plus the blobs the change and the analysis read, and nothing newer: a descendant
cannot leak in because it was never downloaded. That is then checked — `git rev-list --all
--not <cause>` must print nothing — and the result is recorded in the fixture. Replay cannot
look ahead either: a `FixtureSource` answers for its one revision and raises for any other,
and the change set is the cause's own.

What the listing keeps is derived rather than listed, as `tests/fixtures/capture_tree.py`
does it: the adapter's entity globs, the marker files, every template the pipeline parse
actually resolved, every path the static stage read, and the change's own paths. Blobs are
every one the stage read plus every declaration and pipeline definition in the tree, so a
small additive change to what the stage reads still replays.
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..core.static_run import StaticRun
from ..ingest import resolve
from ..models import MODE_RANGE, ChangeSet, ChangeSetSpec
from ..remote import BLOB_FILTER, RemoteRepo, fetch_args
from ..repos import RepoAdapter
from ..repos.base import DirectoryEntitySpec, FileEntitySpec
from ..source import RepoSource, TreeEntry
from ..static.engine import analyze, build_brief
from ..static.fixtures import FixtureError, TreeFixture
from .history import DEFAULT_REMOTE, eval_cache_root, fetch_objects

logger = logging.getLogger(__name__)

ITEM_FIXTURE_VERSION = "1.0"
ITEMS_CACHE_SUBDIR = "items"
CAUSE_REF = "refs/scout/backtest/cause"
CAPTURE_DEPTH = 2
REPO_NAME = "sonic-net/sonic-buildimage"

MARKER_GLOBS = ("slave.mk", "Makefile.work", "rules/config/*", "ansible/testbed-cli.sh", "tests/common/*.py")
BLOB_GLOBS = ("azure-pipelines.yml", ".azure-pipelines/*.yml", "device/*/platform_asic")


@dataclass(frozen=True)
class ItemFixture:
    """One corpus item, pinned: the tree at its cause, the cause's change, and how it was captured."""

    item_id: str
    cause_sha: str
    parent_sha: str
    tree: TreeFixture
    change_set: ChangeSet
    capture: Dict[str, Any] = field(default_factory=dict)

    def source(self) -> RepoSource:
        return self.tree.source()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "item_fixture_version": ITEM_FIXTURE_VERSION,
            "item_id": self.item_id,
            "cause_sha": self.cause_sha,
            "parent_sha": self.parent_sha,
            "capture": dict(self.capture),
            "change_set": self.change_set.to_dict(),
            "tree": self.tree.to_dict(),
        }

    def write(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=1, sort_keys=False) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "ItemFixture":
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise FixtureError(f"Cannot read item fixture {path}: {error}") from error
        version = str(payload.get("item_fixture_version") or "")
        if version != ITEM_FIXTURE_VERSION:
            raise FixtureError(f"{path} is item fixture version {version!r}, this Scout reads {ITEM_FIXTURE_VERSION!r}")
        return cls(
            item_id=str(payload["item_id"]),
            cause_sha=str(payload["cause_sha"]),
            parent_sha=str(payload["parent_sha"]),
            tree=_tree_from_dict(payload["tree"]),
            change_set=ChangeSet.from_dict(payload["change_set"]),
            capture=dict(payload.get("capture") or {}),
        )


def _tree_from_dict(payload: Dict[str, Any]) -> TreeFixture:
    return TreeFixture(
        repo=str(payload["repo"]),
        rev=str(payload["rev"]),
        tree_paths=int(payload["tree_paths"]),
        entries=tuple(TreeEntry(path=item[3], mode=item[0], kind=item[1], sha=item[2])
                      for item in payload.get("entries") or []),
        blobs=dict(payload.get("blobs") or {}),
        path_globs=tuple(payload.get("path_globs") or ()),
        blob_globs=tuple(payload.get("blob_globs") or ()),
        rev_date=str(payload.get("rev_date") or ""),
        captured_at=str(payload.get("captured_at") or ""),
        note=str(payload.get("note") or ""),
        adapter=str(payload.get("adapter") or ""),
    )


class RecordingSource(RepoSource):
    """A live source that keeps a copy of every file the static stage reads through it."""

    def __init__(self, inner: RepoSource) -> None:
        super().__init__()
        self.inner = inner
        self.read: Dict[str, str] = {}

    @property
    def describe(self) -> str:
        return f"recording {self.inner.describe}"

    def git(self, *args: str, check: bool = True, stdin: Optional[str] = None) -> str:
        return self.inner.git(*args, check=check, stdin=stdin)

    def rev_parse(self, revision: str) -> str:
        return self.inner.rev_parse(revision)

    def list_tree(self, commit: str, prefix: str = "") -> List[TreeEntry]:
        return self.inner.list_tree(commit, prefix)

    def path_count(self, commit: str, prefix: str = "") -> int:
        return self.inner.path_count(commit, prefix)

    def read_file(self, commit: str, path: str) -> str:
        content = self.inner.read_file(commit, path)
        self._blob_reads += 1
        self.read[path] = content
        return content


def item_cache(cache_root: Optional[Path], item_id: str, cause_sha: str) -> Path:
    """One directory per item and cause, which is what makes the no-lookahead check meaningful."""
    return eval_cache_root(cache_root) / ITEMS_CACHE_SUBDIR / f"{item_id}-{cause_sha[:12]}"


def capture_item(
    item_id: str,
    cause_sha: str,
    adapter: RepoAdapter,
    remote: str = DEFAULT_REMOTE,
    cache_root: Optional[Path] = None,
    repo_name: str = REPO_NAME,
) -> Tuple[ItemFixture, StaticRun]:
    """Fetch only what `parent..cause` needs, run the static stage live, and pin what it used."""
    started = time.monotonic()
    source = RemoteRepo(remote, cache_root=item_cache(cache_root, item_id, cause_sha))
    source.git(*fetch_args(CAPTURE_DEPTH, [f"+{cause_sha}:{CAUSE_REF}"], BLOB_FILTER))
    fetch_s = time.monotonic() - started

    cause = source.rev_parse(CAUSE_REF)
    if cause != cause_sha:
        raise FixtureError(f"{item_id}: fetched {cause[:12]} for cause {cause_sha[:12]}")
    parents = source.git("log", "-1", "--format=%P", cause).split()
    if not parents:
        raise FixtureError(f"{item_id}: {cause_sha[:12]} is a root commit and has no change of its own to analyze")
    parent = parents[0]

    entries = source.list_tree(cause)
    prefetched = fetch_objects(
        source, {entry.sha for entry in entries if entry.is_file and _matches(entry.path, BLOB_GLOBS)}
    )

    ingest_started = time.monotonic()
    recording = RecordingSource(source)
    spec = ChangeSetSpec(base_ref=parent, head_ref=cause, mode=MODE_RANGE)
    change_set = resolve(spec, recording, adapter)
    ingest_s = time.monotonic() - ingest_started

    run = static_run(recording, change_set, adapter, item_id, repo_name)
    lookahead = check_no_lookahead(source, cause)

    fixture_tree = _pinned_tree(source, cause, entries, adapter, run, change_set, recording.read, repo_name)
    capture = {
        "captured_at": _now(),
        "remote": source.url,
        "cache": str(source.cache_dir),
        "fetch": {"depth": CAPTURE_DEPTH, "filter": BLOB_FILTER, "refspec": f"+{cause_sha}:{CAUSE_REF}"},
        "fetch_s": round(fetch_s, 3),
        "ingest_s": round(ingest_s, 3),
        "static_duration_s": round(run.duration_s, 3),
        "blobs_read": run.blobs_read,
        "blobs_prefetched": prefetched,
        "total_s": round(time.monotonic() - started, 3),
        "cache_bytes": source.cache_size_bytes(),
        "lookahead": lookahead,
        "brief_sha": run.brief.sha(),
        "coverage": coverage_sets(run.brief),
    }
    fixture = ItemFixture(item_id=item_id, cause_sha=cause, parent_sha=parent, tree=fixture_tree,
                          change_set=change_set, capture=capture)
    logger.info(
        "Captured %s at %s: fetch %.2fs, ingest %.2fs, static %.3fs, %d blob read(s); %d commit(s) in store, "
        "%d after the cause",
        item_id, cause[:12], fetch_s, ingest_s, run.duration_s, run.blobs_read,
        lookahead["commits_in_store"], lookahead["descendants_in_store"],
    )
    return fixture, run


def check_no_lookahead(source: RepoSource, cause_sha: str) -> Dict[str, Any]:
    """What the object store holds beyond the cause's own history; must be nothing."""
    beyond = source.git("rev-list", "--all", "--not", cause_sha).split()
    held = source.git("rev-list", "--all").split()
    if beyond:
        raise FixtureError(
            f"The capture cache for {cause_sha[:12]} holds {len(beyond)} commit(s) that are not ancestors of the "
            f"cause, e.g. {beyond[0][:12]}; a backtest run against it could see the future"
        )
    return {
        "check": f"git rev-list --all --not {cause_sha}",
        "descendants_in_store": len(beyond),
        "commits_in_store": len(held),
        "commits": held,
    }


def replay_item(fixture: ItemFixture, adapter: RepoAdapter, repo_name: str = REPO_NAME) -> StaticRun:
    """Run the static stage over a pinned item. No git, no network, one revision."""
    return static_run(fixture.source(), fixture.change_set, adapter, fixture.item_id, repo_name)


def static_run(source: RepoSource, change_set: ChangeSet, adapter: RepoAdapter, item_id: str,
               repo_name: str = REPO_NAME) -> StaticRun:
    """`run_static` over an already-resolved change set, which is what makes replay offline."""
    rev = change_set.head_sha
    result = analyze(source, rev, adapter, change_set=change_set)
    brief = build_brief(
        result,
        repo=repo_name,
        base_sha=change_set.base_sha,
        head_sha=rev,
        mode=MODE_RANGE,
        run_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"scout-backtest:{item_id}:{rev}")),
        measured_at=_now(),
    )
    return StaticRun(brief=brief, result=result, change_set=change_set)


def coverage_sets(brief: Any) -> Dict[str, List[str]]:
    coverage = brief.coverage
    return {key: list(coverage.get(key) or []) for key in ("affected", "covered", "uncovered", "ambiguous")}


def capture_globs(adapter: RepoAdapter, run: StaticRun) -> Tuple[List[str], List[str]]:
    """Listing and blob globs for one item, derived from the adapter and from what the run parsed."""
    model = adapter.entity_model
    globs: List[str] = list(MARKER_GLOBS)
    if isinstance(model, DirectoryEntitySpec):
        globs.append(f"{model.root}/*/{model.declaration_file}")
        if model.identity_marker:
            globs.append(f"{model.root}/*/{model.identity_marker}")
        globs.extend(f"{model.root}/*/{marker}" for marker in model.hwsku_markers)
    elif isinstance(model, FileEntitySpec):
        globs.append(model.glob)
    coverage_model = run.result.model
    if coverage_model is not None:
        globs.append(coverage_model.path)
        globs.extend(coverage_model.templates_read)
    return sorted(set(globs)), sorted(set(BLOB_GLOBS))


def _pinned_tree(
    source: RepoSource,
    rev: str,
    entries: Sequence[TreeEntry],
    adapter: RepoAdapter,
    run: StaticRun,
    change_set: ChangeSet,
    read: Dict[str, str],
    repo_name: str,
) -> TreeFixture:
    path_globs, blob_globs = capture_globs(adapter, run)
    wanted = set(read) | set(change_set.changed_paths)
    kept = {entry.path: entry for entry in entries
            if entry.path in wanted or _matches(entry.path, path_globs) or _matches(entry.path, blob_globs)}

    blobs: Dict[str, str] = {}
    for path, content in read.items():
        entry = kept.get(path)
        if entry is not None:
            blobs[entry.sha] = content
    for entry in kept.values():
        if entry.is_file and entry.sha not in blobs and _matches(entry.path, blob_globs):
            blobs[entry.sha] = source.read_file(rev, entry.path)

    return TreeFixture(
        repo=repo_name,
        adapter=adapter.name,
        rev=rev,
        rev_date=source.git("log", "-1", "--format=%cI", rev).strip(),
        captured_at=_now(),
        note=f"backtest item fixture: the tree at the cause, for {change_set.base_sha[:12]}..{rev[:12]}",
        tree_paths=len(entries),
        entries=tuple(sorted(kept.values(), key=lambda item: item.path)),
        blobs=blobs,
        path_globs=tuple(path_globs),
        blob_globs=tuple(blob_globs),
    )


def _matches(path: str, globs: Sequence[str]) -> bool:
    return any(fnmatchcase(path, pattern) for pattern in globs)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
