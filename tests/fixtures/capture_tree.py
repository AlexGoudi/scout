#!/usr/bin/env python3
"""Capture a pinned tree fixture for the conformance suite (HLD section 4.8, NFR-10).

Run once per tree, by hand, against a source that can reach the revision — a remote, or a
checkout that already has it. The output is committed and the suite reads it offline
forever after; nothing in a test run ever calls this.

    python3 tests/fixtures/capture_tree.py \\
        --remote sonic-net/sonic-buildimage --rev 62cfe5086e4ffae105c506093475c3a50424bb4c \\
        --out tests/fixtures/trees/sonic-buildimage-master-62cfe50.json

What gets captured is derived from the adapter rather than listed here: the declaration,
HWSKU-marker and identity paths under its entity root, the marker files adapter detection
needs, and — crucially — **the pipeline templates the parser actually resolves through**,
discovered by running the parser during capture rather than by guessing at a glob. If the
template chain moves, the next capture follows it and the committed fixture stops
matching, which is the failure showing up as a diff instead of as a wrong number.
"""

import argparse
import sys
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Dict, List, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scout_impl.remote import RemoteRepo  # noqa: E402
from scout_impl.repos import RepoAdapter, get_adapter  # noqa: E402
from scout_impl.repos.base import DirectoryEntitySpec, FileEntitySpec  # noqa: E402
from scout_impl.source import LocalCheckout, RepoSource, TreeEntry  # noqa: E402
from scout_impl.static.extract import build_coverage  # noqa: E402
from scout_impl.static.fixtures import TreeFixture  # noqa: E402
from scout_impl.static.treeindex import SYMLINK_MODE, TreeIndex  # noqa: E402

# Marker files adapter detection needs; without them a fixture cannot identify itself.
MARKER_GLOBS = ("slave.mk", "Makefile.work", "rules/config/*", "ansible/testbed-cli.sh", "tests/common/*.py")


def _entity_globs(adapter: RepoAdapter) -> List[str]:
    model = adapter.entity_model
    if isinstance(model, FileEntitySpec):
        return [model.glob]
    if isinstance(model, DirectoryEntitySpec):
        globs = [f"{model.root}/*/{model.declaration_file}"]
        if model.identity_marker:
            globs.append(f"{model.root}/*/{model.identity_marker}")
        globs.extend(f"{model.root}/*/{marker}" for marker in model.hwsku_markers)
        return globs
    return []


def _entity_blob_globs(adapter: RepoAdapter) -> List[str]:
    """Only a directory-shaped entity has a declaration to read; a file-shaped one does not."""
    model = adapter.entity_model
    if isinstance(model, DirectoryEntitySpec):
        return [f"{model.root}/*/{model.declaration_file}"]
    return []


def _coverage_paths(source: RepoSource, rev: str, adapter: RepoAdapter) -> List[str]:
    """The coverage definition plus every template the strict parse reaches, found by parsing."""
    spec = adapter.coverage_spec
    if spec is None:
        return []
    model = build_coverage(TreeIndex(source, rev), spec)
    return [spec.path, *model.templates_read]


def _matching(paths: Sequence[str], globs: Sequence[str]) -> List[str]:
    return [path for path in paths if any(fnmatchcase(path, pattern) for pattern in globs)]


def _symlinks_under(entries: Sequence[TreeEntry], adapter: RepoAdapter) -> Dict[str, TreeEntry]:
    """Every mode-120000 entry under the entity root, for rule C6. Captured by mode, not glob."""
    model = adapter.entity_model
    if not isinstance(model, DirectoryEntitySpec) or not (model.reverse_reach or model.alias_directories):
        return {}
    prefix = model.root + "/"
    return {entry.path: entry for entry in entries
            if entry.mode == SYMLINK_MODE and entry.path.startswith(prefix)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--remote", help="owner/repo or a URL to fetch the revision from")
    group.add_argument("--checkout", help="A working copy or partial clone that already holds the revision")
    parser.add_argument("--rev", required=True, help="Revision to pin; the fixture holds this one and no other")
    parser.add_argument("--repo-type", help="Adapter name; identified from the tree when omitted")
    parser.add_argument("--repo", help="owner/repo recorded in the fixture; defaults to --remote")
    parser.add_argument("--out", required=True, help="Where to write the fixture JSON")
    parser.add_argument("--note", default="", help="Why this revision was pinned")
    parser.add_argument("--depth", type=int, default=2, help="Fetch depth when --remote is used")
    parser.add_argument("--path-glob", action="append", default=[], help="Extra paths to list; `*` spans separators")
    parser.add_argument("--blob-glob", action="append", default=[], help="Extra blobs to capture")
    args = parser.parse_args()

    if args.remote:
        source: RepoSource = RemoteRepo(args.remote, depth=args.depth)
        source.fetch_range(args.rev, args.rev, merge_base=False)
    else:
        source = LocalCheckout(Path(args.checkout).expanduser().resolve())

    rev = source.rev_parse(args.rev)
    entries = source.list_tree(rev)
    adapter = get_adapter(args.repo_type) if args.repo_type else source.detect_adapter(rev)
    coverage_paths = _coverage_paths(source, rev, adapter)

    path_globs = sorted(set(
        _entity_globs(adapter) + list(MARKER_GLOBS) + coverage_paths + args.path_glob
    ))
    blob_globs = sorted(set(_entity_blob_globs(adapter) + coverage_paths + args.blob_glob))

    kept = {entry.path: entry for entry in entries if _matching([entry.path], path_globs)}

    # Rule C6 needs every symlink under the entity root, and needs each one's target, so
    # both the entries and their blobs are captured by **mode** rather than by glob — the
    # question "which platforms does this shared file belong to" is asked of paths the
    # declaration globs have no reason to name. They dedupe hard: on upstream, 1,853 links
    # hold 493 distinct targets between them, and the fixture stores the 493.
    links = _symlinks_under(entries, adapter)
    kept.update(links)
    wanted = sorted(set(_matching(sorted(kept), blob_globs)) | set(links))

    source.prefetch_blobs([entry.sha for entry in kept.values()])
    blobs = {}
    for path in wanted:
        entry = kept[path]
        if entry.sha not in blobs:
            blobs[entry.sha] = source.read_file(rev, path)

    fixture = TreeFixture(
        repo=args.repo or args.remote or adapter.name,
        adapter=adapter.name,
        rev=rev,
        rev_date=source.git("log", "-1", "--format=%cI", rev).strip(),
        captured_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        note=args.note,
        tree_paths=len(entries),
        entries=tuple(sorted(kept.values(), key=lambda item: item.path)),
        blobs=blobs,
        path_globs=tuple(path_globs),
        blob_globs=tuple(blob_globs),
    )
    fixture.write(Path(args.out))

    print(f"{args.out}: {adapter.name} at {rev[:9]} ({fixture.rev_date})")
    print(f"  tree paths        : {fixture.tree_paths}")
    print(f"  listed entries    : {len(fixture.entries)}")
    print(f"  distinct blobs    : {len(fixture.blobs)} from {len(wanted)} path(s)")
    print(f"  blob reads spent  : {source.blob_reads}")
    print(f"  size on disk      : {Path(args.out).stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
