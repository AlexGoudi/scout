"""One interface over the two places a change set can come from.

Ingest and the analyzers take a `RepoSource` and never ask whether it is a working copy
on disk or a partial clone fetched from a remote nobody has cloned, which is what lets
the same code run against a developer's checkout and against a pull request.

**The two read operations are deliberately not symmetric in cost, and the API says so.**
`list_tree` and the helpers over it answer presence-and-absence questions from tree
metadata, which a `--filter=blob:none` fetch already holds in full: they are local,
they are milliseconds, and they transfer nothing. `read_file` pulls file content, which
on such a source is one network round trip per blob, and `blob_reads` counts them so a
caller can see what it spent. Anything that can be phrased as "which paths exist" should
be phrased that way — enumerating the HWSKU directories that carry a `port_config.ini`
but no `pg_profile_lookup.ini` is a `paths_matching` call against 15,000 tree entries
and costs nothing beyond the fetch that was already paid for.
"""

import abc
import posixpath
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import List, Optional, Sequence

from .gitcmd import GitError, GitRepo
from .repos import RepoAdapter, resolve_adapter

KIND_BLOB = "blob"
KIND_TREE = "tree"
KIND_COMMIT = "commit"


@dataclass(frozen=True)
class TreeEntry:
    """One entry of a tree listing: mode, kind, object id and path. Never content."""

    path: str
    mode: str
    kind: str
    sha: str

    @property
    def is_file(self) -> bool:
        return self.kind == KIND_BLOB

    @property
    def is_submodule(self) -> bool:
        """A submodule pointer, which a recursive listing reports without descending."""
        return self.kind == KIND_COMMIT


class RepoSource(abc.ABC):
    """A read-only repository Scout can resolve revisions, trees and files against."""

    def __init__(self) -> None:
        self._blob_reads = 0

    @property
    @abc.abstractmethod
    def describe(self) -> str:
        """Where this source reads from, for logs and error messages."""

    @abc.abstractmethod
    def git(self, *args: str, check: bool = True, stdin: Optional[str] = None) -> str:
        """Run one read-only git command against this source and return its stdout."""

    @property
    def blob_reads(self) -> int:
        """How many file contents have been read; on a remote source, round trips."""
        return self._blob_reads

    def rev_parse(self, revision: str) -> str:
        """Resolve a revision to a full commit sha, raising if it does not exist."""
        resolved = self.git("rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}", check=False).strip()
        if not resolved:
            raise GitError(f"Revision not found in {self.describe}: {revision}")
        return resolved

    def merge_base(self, left: str, right: str) -> str:
        output = self.git("merge-base", left, right).strip()
        if not output:
            raise GitError(f"No merge base between {left} and {right} in {self.describe}")
        return output

    def list_tree(self, commit: str, prefix: str = "") -> List[TreeEntry]:
        """Every path at `commit`, recursively. Cheap: tree metadata, no file content.

        Object sizes are not reported and `-l` is not passed, because a blob-filtered
        source can only answer a size by downloading the blob it was filtered to avoid.
        """
        args = ["ls-tree", "-r", "-z", "--full-tree", commit]
        if prefix:
            args += ["--", prefix]

        entries: List[TreeEntry] = []
        for record in self.git(*args).split("\0"):
            if not record:
                continue
            metadata, _, path = record.partition("\t")
            mode, kind, sha = metadata.split()
            entries.append(TreeEntry(path=path, mode=mode, kind=kind, sha=sha))
        return entries

    def list_paths(self, commit: str, prefix: str = "") -> List[str]:
        """Every path at `commit`, recursively. Cheap: no file content is transferred."""
        return [entry.path for entry in self.list_tree(commit, prefix)]

    def path_count(self, commit: str, prefix: str = "") -> int:
        """How many paths the tree holds at `commit`. Cheap, like `list_tree`.

        Separate from `len(list_paths(...))` only so a source that carries a deliberately
        filtered listing — a pinned test fixture — can still answer the size of the tree
        it was captured from, which is a measurement the risk brief reports.
        """
        return len(self.list_paths(commit, prefix))

    def paths_matching(self, commit: str, pattern: str, prefix: str = "") -> List[str]:
        """Paths at `commit` matching a glob, where `*` spans directory separators.

        Cheap, like `list_tree`. This is the operation a presence-and-absence question
        should be built from rather than reading files to find out what is in them.
        """
        return [path for path in self.list_paths(commit, prefix) if fnmatchcase(path, pattern)]

    def directories_containing(self, commit: str, filename: str, prefix: str = "") -> List[str]:
        """Directories at `commit` holding a file of this name. Cheap, like `list_tree`.

        The coverage-gap shape in one call: the directories carrying one marker file
        minus those carrying another is a set difference over two of these.
        """
        return sorted(
            {posixpath.dirname(path) for path in self.paths_matching(commit, f"*{filename}", prefix)
             if posixpath.basename(path) == filename}
        )

    def read_file(self, commit: str, path: str) -> str:
        """Content of one file at `commit`. **Expensive** on a remote source: this is
        the operation that defeats `--filter=blob:none`, one network round trip per
        blob. Prefer `paths_matching` wherever the question is about paths."""
        self._blob_reads += 1
        return self.git("cat-file", "-p", f"{commit}:{path}")

    def prefetch_blobs(self, shas: Sequence[str]) -> int:
        """Bring these blobs within reach of `read_file` in as few round trips as possible.

        A no-op by default, and correctly so: a working copy and a pinned fixture already
        hold everything they can answer, so there is nothing to bring. It exists because a
        blob-filtered remote does not, and because an analyzer that has to read hundreds of
        small blobs — every symlink target under `device/`, say — would otherwise pay a
        round trip for each one. Returns how many were actually fetched.

        Callers must treat this as advisory. A source that cannot prefetch, or a fetch that
        fails, leaves `read_file` to do exactly what it did before: the result is slower,
        never different.
        """
        return 0

    def detect_adapter(self, commit: str) -> RepoAdapter:
        """Identify which repository this is from the tree at `commit`. Cheap."""
        return resolve_adapter(paths=self.list_paths(commit))


class LocalCheckout(RepoSource):
    """A source backed by a working copy already on disk. Never touches the network."""

    def __init__(self, root: Path, timeout_seconds: int = 300) -> None:
        super().__init__()
        self.repo = GitRepo(root, timeout_seconds=timeout_seconds)

    @property
    def root(self) -> Path:
        return self.repo.root

    @property
    def describe(self) -> str:
        return str(self.repo.root)

    def git(self, *args: str, check: bool = True, stdin: Optional[str] = None) -> str:
        return self.repo.run(*args, check=check, stdin=stdin)


def as_source(source: object) -> RepoSource:
    """Accept a `RepoSource` or a path to a checkout, and return a `RepoSource`."""
    if isinstance(source, RepoSource):
        return source
    if isinstance(source, (str, Path)):
        return LocalCheckout(Path(source))
    raise TypeError(f"Not a repository source or a path to one: {source!r}")
