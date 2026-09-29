"""Pinned tree fixtures: a `RepoSource` that never touches git or the network (NFR-10).

The conformance suite is what replaces a verification stage for the committed detector
(HLD section 4.8), so it has to run everywhere, every time, with no clone and no remote.
A fixture holds three things, which section 4.2 says is all the static stage needs: a
filtered tree listing carrying paths, modes and blob shas; the handful of blobs the stage
actually reads; and the size of the tree it was captured from.

Two decisions worth stating. The listing is **filtered** rather than complete — 2,000
paths instead of 20,155 — because presence-and-absence questions are the only thing the
stage asks of it, and the globs that were captured are recorded in the fixture so a
reader can see which questions it can answer. Blobs are keyed by **blob sha**, not by
path, which is not just compression: the 287 `platform_asic` declarations upstream hold
only 21 distinct blobs, and storing them by content is the same fact that makes
`TreeIndex` read them 21 times rather than 287.

A blob the fixture does not carry raises rather than returning empty. A fixture that has
drifted out of step with what the analyzer reads is a test failure, and the one failure
mode worth spending an exception on is the one that would otherwise be silent.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from ..source import RepoSource, TreeEntry

# 1.1 writes one tree entry per line in `git ls-tree` order — "mode kind sha path" — where
# 1.0 wrote a four-element array. The same bytes, a sixth of the lines, and a diff a
# reviewer can read: rule C6 needs every symlink under `device/`, which took the upstream
# fixture from 1,255 entries to 3,065, and at six lines apiece that was 19,000 lines of
# brackets.
FIXTURE_VERSION = "1.1"


class FixtureError(RuntimeError):
    """A pinned fixture is unreadable, or does not carry what was asked of it."""


@dataclass(frozen=True)
class TreeFixture:
    """One tree captured at one revision, and what was captured of it."""

    repo: str
    rev: str
    tree_paths: int
    entries: Tuple[TreeEntry, ...]
    blobs: Dict[str, str]
    path_globs: Tuple[str, ...] = ()
    blob_globs: Tuple[str, ...] = ()
    rev_date: str = ""
    captured_at: str = ""
    note: str = ""
    adapter: str = ""

    @classmethod
    def from_files(
        cls,
        files: Dict[str, Union[str, Tuple[str, str]]],
        repo: str = "test/repo",
        rev: str = "0" * 40,
        adapter: str = "",
        tree_paths: Optional[int] = None,
        note: str = "built in memory",
    ) -> "TreeFixture":
        """An in-memory tree, for a test that wants one shape rather than a whole repository.

        `files` maps a path to its content, or to a `(mode, content)` pair when the mode
        matters — `120000` for a symlink, whose content is its target. Blob shas are hashes
        of the content, so two identical files share one blob exactly as git would, which
        is the property `TreeIndex`'s read memo is keyed on and therefore the one a test of
        that memo must not fake.
        """
        entries = []
        blobs = {}
        for path, value in files.items():
            mode, content = value if isinstance(value, tuple) else ("100644", value)
            sha = hashlib.sha1(content.encode("utf-8")).hexdigest()
            entries.append(TreeEntry(path=path, mode=mode, kind="blob", sha=sha))
            blobs[sha] = content

        return cls(
            repo=repo,
            adapter=adapter,
            rev=rev,
            tree_paths=tree_paths if tree_paths is not None else len(entries),
            entries=tuple(entries),
            blobs=blobs,
            note=note,
        )

    @classmethod
    def load(cls, path: Path) -> "TreeFixture":
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise FixtureError(f"Cannot read tree fixture {path}: {error}") from error

        version = str(payload.get("fixture_version") or "")
        if version != FIXTURE_VERSION:
            raise FixtureError(f"{path} is fixture version {version!r}, this Scout reads {FIXTURE_VERSION!r}")
        return cls.from_dict(payload)

    @classmethod
    def from_dict(cls, payload: Dict[str, object]) -> "TreeFixture":
        return cls(
            repo=str(payload["repo"]),
            rev=str(payload["rev"]),
            tree_paths=int(payload["tree_paths"]),
            entries=tuple(_entry(item) for item in payload.get("entries") or []),
            blobs=dict(payload.get("blobs") or {}),
            path_globs=tuple(payload.get("path_globs") or ()),
            blob_globs=tuple(payload.get("blob_globs") or ()),
            rev_date=str(payload.get("rev_date") or ""),
            captured_at=str(payload.get("captured_at") or ""),
            note=str(payload.get("note") or ""),
            adapter=str(payload.get("adapter") or ""),
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "fixture_version": FIXTURE_VERSION,
            "repo": self.repo,
            "adapter": self.adapter,
            "rev": self.rev,
            "rev_date": self.rev_date,
            "captured_at": self.captured_at,
            "note": self.note,
            "tree_paths": self.tree_paths,
            "path_globs": list(self.path_globs),
            "blob_globs": list(self.blob_globs),
            "entries": [f"{entry.mode} {entry.kind} {entry.sha} {entry.path}"
                        for entry in sorted(self.entries, key=lambda item: item.path)],
            "blobs": dict(sorted(self.blobs.items())),
        }

    def write(self, path: Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=1, sort_keys=False) + "\n", encoding="utf-8")

    def source(self) -> "FixtureSource":
        return FixtureSource(self)


class FixtureSource(RepoSource):
    """A read-only source served entirely from a pinned fixture. No git, no network."""

    def __init__(self, fixture: TreeFixture) -> None:
        super().__init__()
        self.fixture = fixture
        self._by_path = {entry.path: entry for entry in fixture.entries}

    @property
    def describe(self) -> str:
        return f"pinned fixture {self.fixture.repo}@{self.fixture.rev[:9]}"

    def git(self, *args: str, check: bool = True, stdin: Optional[str] = None) -> str:
        raise FixtureError(
            f"{self.describe} is offline by construction and was asked to run `git {' '.join(args)}`; "
            f"an analyzer reaching for git behind the source API is the thing NFR-10 exists to prevent"
        )

    def rev_parse(self, revision: str) -> str:
        if revision in ("HEAD", "FETCH_HEAD", self.fixture.rev) or self.fixture.rev.startswith(revision):
            return self.fixture.rev
        raise FixtureError(f"{self.describe} holds one revision and it is not {revision!r}")

    def list_tree(self, commit: str, prefix: str = "") -> List[TreeEntry]:
        self._check_rev(commit)
        entries = sorted(self.fixture.entries, key=lambda item: item.path)
        if prefix:
            entries = [entry for entry in entries
                       if entry.path == prefix or entry.path.startswith(prefix.rstrip("/") + "/")]
        return entries

    def path_count(self, commit: str, prefix: str = "") -> int:
        self._check_rev(commit)
        if prefix:
            return len(self.list_tree(commit, prefix))
        return self.fixture.tree_paths

    def read_file(self, commit: str, path: str) -> str:
        self._check_rev(commit)
        entry = self._by_path.get(path)
        if entry is None:
            raise FixtureError(f"{self.describe} has no path {path!r} in its filtered listing")
        content = self.fixture.blobs.get(entry.sha)
        if content is None:
            raise FixtureError(
                f"{self.describe} lists {path!r} but did not capture its blob {entry.sha[:9]}. Recapture the "
                f"fixture with a blob glob that covers it; the analyzer now reads a file the fixture does not "
                f"carry, and answering with nothing would make that drift invisible"
            )
        self._blob_reads += 1
        return content

    def _check_rev(self, commit: str) -> None:
        if commit != self.fixture.rev:
            raise FixtureError(f"{self.describe} was asked for {commit[:9]}, which it does not hold")


def _entry(record: str) -> TreeEntry:
    """Read one `mode kind sha path` line. Split three times: a path may contain spaces."""
    mode, kind, sha, path = str(record).split(" ", 3)
    return TreeEntry(path=path, mode=mode, kind=kind, sha=sha)


def load_fixture(path: Path) -> TreeFixture:
    return TreeFixture.load(Path(path))
