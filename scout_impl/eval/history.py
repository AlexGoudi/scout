"""Revert history for the seed corpus, fetched without a clone (HLD section 6.3).

Mining needs commit messages and changed paths, never file content, so the history is a
**treeless** fetch of the whole of master: 13,105 commits in 2.9 s and 10.8 MB, measured
anonymously on 24 Sep 2026. The trees a name-only walk needs are fetched afterwards, in one
batch, for exactly the commits that walk will diff; left to itself, git would fetch them
lazily at one round trip per commit.

The cache is a separate directory from the one pull requests are fetched into, for two
reasons. A pull-request fetch passes `--depth`, and `git fetch --depth` shortens a history
as readily as it deepens one, so a `review` running alongside would truncate master under
the miner. And two fetches into one bare repository contend for the same lock files.

Rename detection is pinned off. In a partial clone it is the one part of a name-only walk
that reads blob content — a lazy fetch per rename candidate — and a rename reported as a
delete plus an add names both paths, which is what ground truth wants anyway.
"""

import logging
import time
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from ..gitcmd import GitError, GitRepo
from ..incidents import DEFAULT_GREP, Incident, mine_incidents
from ..mining.message import REVERTS_SHA_RE
from ..remote import BLOB_FILTER, COMMIT_FILTER, RemoteRepo, default_cache_root
from ..source import LocalCheckout, RepoSource

logger = logging.getLogger(__name__)

DEFAULT_REMOTE = "sonic-net/sonic-buildimage"
DEFAULT_BRANCH = "refs/heads/master"
HISTORY_REF = "refs/scout/history/master"
EVAL_CACHE_SUBDIR = "eval"
HISTORY_CACHE_SUBDIR = "history"
# Tried in order in a fallback clone: a fork with upstream fetched has the truest master.
FALLBACK_REFS = ("upstream/master", "origin/master", "master")
PREFETCH_BATCH = 2000

SOURCE_REMOTE = "remote"
SOURCE_CHECKOUT = "checkout"

_RECORD_SEPARATOR = "\x1e"
_FIELD_SEPARATOR = "\x1f"


class HistoryError(GitError):
    """History could be obtained neither from the remote nor from any fallback clone."""


class HistoryRepo(GitRepo):
    """A `GitRepo` whose name-only walks never read blob content."""

    def run(self, *args: str, check: bool = True, stdin: Optional[str] = None) -> str:
        return super().run("-c", "diff.renames=false", *args, check=check, stdin=stdin)


@dataclass(frozen=True)
class History:
    """Master pinned at one tip, where it came from, and the repository to walk it in."""

    repo: HistoryRepo
    revision: str
    source: str
    location: str
    ref: str
    remote: Optional[RemoteRepo] = None
    fetch_s: float = 0.0
    fallback_reason: str = ""

    @property
    def partial(self) -> bool:
        """Trees and blobs arrive on demand, so they have to be fetched before a walk needs them."""
        return self.remote is not None

    def tree_source(self) -> RepoSource:
        return self.remote if self.remote is not None else LocalCheckout(self.repo.root)

    def commit_date(self, sha: str) -> str:
        return self.repo.run("log", "-1", "--format=%cI", sha).strip()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "location": self.location,
            "ref": self.ref,
            "revision": self.revision,
            "revision_date": self.commit_date(self.revision),
            "partial": self.partial,
            "fetch_s": round(self.fetch_s, 3),
            "fallback_reason": self.fallback_reason or None,
        }


def eval_cache_root(cache_root: Optional[Path] = None) -> Path:
    """Where the harness keeps its own fetches: beside the review cache, never inside it."""
    return (Path(cache_root) if cache_root is not None else default_cache_root()) / EVAL_CACHE_SUBDIR


def open_history(
    remote: str = DEFAULT_REMOTE,
    cache_root: Optional[Path] = None,
    revision: Optional[str] = None,
    fallback_checkouts: Sequence[Path] = (),
) -> History:
    """Fetch master treeless into the eval cache, falling back to a local clone only on failure.

    `revision` pins the tip, so a corpus mined today can be mined again byte for byte after
    master has moved on; it must be on master's history.
    """
    try:
        return _fetch_history(remote, cache_root, revision)
    except GitError as error:
        if not fallback_checkouts:
            raise
        reason = f"{type(error).__name__}: {error}"
        logger.warning("Fetching history from %s failed; falling back to a local clone. %s", remote, reason)
        return _checkout_history(fallback_checkouts, revision, reason)


def _fetch_history(remote: str, cache_root: Optional[Path], revision: Optional[str]) -> History:
    repo = RemoteRepo(remote, cache_root=eval_cache_root(cache_root) / HISTORY_CACHE_SUBDIR)
    args = ["fetch", "--quiet", "--no-tags", f"--filter={COMMIT_FILTER}"]
    if (repo.cache_dir / "shallow").is_file():
        args.append("--unshallow")

    started = time.monotonic()
    repo.git(*args, "origin", f"+{DEFAULT_BRANCH}:{HISTORY_REF}")
    elapsed = time.monotonic() - started

    history_repo = HistoryRepo(repo.cache_dir)
    pinned = _pin(history_repo, history_repo.rev_parse(HISTORY_REF), revision, DEFAULT_BRANCH)
    logger.info(
        "Fetched %s treeless from %s in %.2fs; %.1f MB cached; mining at %s",
        DEFAULT_BRANCH,
        repo.url,
        elapsed,
        repo.cache_size_bytes() / 1e6,
        pinned[:12],
    )
    return History(
        repo=history_repo,
        revision=pinned,
        source=SOURCE_REMOTE,
        location=repo.url,
        ref=DEFAULT_BRANCH,
        remote=repo,
        fetch_s=elapsed,
    )


def _checkout_history(checkouts: Sequence[Path], revision: Optional[str], reason: str) -> History:
    tried: List[str] = []
    for checkout in checkouts:
        root = Path(checkout).expanduser()
        if not root.is_dir():
            tried.append(f"{root}: not a directory")
            continue
        repo = HistoryRepo(root)
        for ref in FALLBACK_REFS:
            tip = repo.run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False).strip()
            if not tip:
                continue
            pinned = _pin(repo, tip, revision, ref)
            logger.warning("Mining the local clone %s at %s (%s) instead of the remote", root, ref, pinned[:12])
            return History(
                repo=repo,
                revision=pinned,
                source=SOURCE_CHECKOUT,
                location=str(root),
                ref=ref,
                fallback_reason=reason,
            )
        tried.append(f"{root}: none of {', '.join(FALLBACK_REFS)}")
    raise HistoryError(f"No history available. The remote failed ({reason}) and no fallback clone served: {tried}")


def _pin(repo: GitRepo, tip: str, revision: Optional[str], ref: str) -> str:
    if not revision:
        return tip
    pinned = repo.rev_parse(revision)
    if repo.run("merge-base", pinned, tip, check=False).strip() != pinned:
        raise HistoryError(f"{revision} is not on the history of {ref} at {tip[:12]}, so it cannot pin a corpus")
    return pinned


def local_objects(remote: RemoteRepo) -> Set[str]:
    """Every object the cache holds. Enumerating what is present never fetches what is not."""
    return set(remote.git("cat-file", "--batch-all-objects", "--batch-check=%(objectname)").split())


def fetch_objects(remote: RemoteRepo, oids: Iterable[str]) -> int:
    """Bring trees or blobs into a partial clone in as few round trips as possible.

    This is the request git makes when it lazily fetches one missing object — negotiation
    off, blob filter on, object ids on stdin — made once for the whole set. Negotiation has
    to be off: the cache already holds the commits these objects hang from, and a server
    told so would assume it holds their trees as well. Explicitly wanted objects are exempt
    from the filter, and a wanted tree arrives with its subtrees.
    """
    missing = sorted(set(oids) - local_objects(remote))
    for start in range(0, len(missing), PREFETCH_BATCH):
        batch = missing[start:start + PREFETCH_BATCH]
        remote.git(
            "-c", "fetch.negotiationAlgorithm=noop",
            "fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "--recurse-submodules=no",
            f"--filter={BLOB_FILTER}", "--stdin", "origin",
            stdin="\n".join(batch) + "\n",
        )
    return len(missing)


def parents_of(repo: GitRepo, commits: Sequence[str]) -> List[str]:
    if not commits:
        return []
    output = repo.run("log", "--no-walk", "--stdin", "--format=%P", stdin="\n".join(commits) + "\n")
    return sorted({parent for line in output.splitlines() for parent in line.split()})


def ensure_trees(history: History, commits: Iterable[str], parents: bool = True) -> int:
    """Make the root trees of `commits`, and of their parents, local. Returns how many were fetched."""
    wanted = sorted(set(commits))
    if not history.partial or not wanted:
        return 0
    if parents:
        wanted = sorted(set(wanted) | set(parents_of(history.repo, wanted)))
    trees = history.repo.run("log", "--no-walk", "--stdin", "--format=%T", stdin="\n".join(wanted) + "\n").split()
    return fetch_objects(history.remote, trees)


def ensure_blobs(history: History, commits: Iterable[str], patterns: Sequence[str]) -> int:
    """Make the blobs matching `patterns` at each commit local, in one batch across all of them."""
    if not history.partial:
        return 0
    source = history.tree_source()
    oids = {
        entry.sha
        for commit in sorted(set(commits))
        for entry in source.list_tree(commit)
        if entry.is_file and any(fnmatchcase(entry.path, pattern) for pattern in patterns)
    }
    return fetch_objects(history.remote, oids)


def master_commits(history: History) -> Set[str]:
    """Every commit on the pinned tip's history. Commit objects only, so nothing is fetched."""
    return set(history.repo.run("rev-list", history.revision).split())


def revert_graph(history: History, grep: str = DEFAULT_GREP) -> Dict[str, List[str]]:
    """The reverts the miner will find, and the causes they name, from commit messages alone."""
    output = history.repo.run("log", f"--grep={grep}", f"--format={_RECORD_SEPARATOR}%H{_FIELD_SEPARATOR}%B",
                              history.revision)
    reverts: List[str] = []
    referenced: Set[str] = set()
    for record in output.split(_RECORD_SEPARATOR):
        if not record.strip():
            continue
        sha, _, body = record.partition(_FIELD_SEPARATOR)
        reverts.append(sha.strip())
        referenced.update(REVERTS_SHA_RE.findall(body))
    on_master = master_commits(history)
    resolved = {sha for sha in _resolve_on(on_master, referenced)}
    return {"reverts": reverts, "causes": sorted(resolved)}


def _resolve_on(commits: Set[str], names: Iterable[str]) -> List[str]:
    """Resolve full or abbreviated shas against a known commit set, without asking git.

    Asking git would not be neutral in a partial clone: a sha it does not hold is fetched
    from the remote, which will serve a commit from any branch, and a cause that was never
    on master would then resolve as though it were.
    """
    resolved = []
    for name in names:
        matches = [sha for sha in commits if sha.startswith(name)] if len(name) < 40 else (
            [name] if name in commits else [])
        if len(matches) == 1:
            resolved.append(matches[0])
    return resolved


def mine(history: History, grep: str = DEFAULT_GREP) -> List[Incident]:
    """Run the incident miner at the pinned tip, fetching only the trees its walk diffs."""
    if history.partial:
        graph = revert_graph(history, grep)
        fetched = ensure_trees(history, graph["reverts"] + graph["causes"])
        logger.info(
            "Prefetched %d tree(s) for %d revert(s) and %d cause(s) on master",
            fetched,
            len(graph["reverts"]),
            len(graph["causes"]),
        )
    return mine_incidents(history.repo, revision=history.revision, grep=grep)


def changed_paths(history: History, commits: Sequence[str]) -> Dict[str, List[str]]:
    """First-parent changed paths per commit; a merge commit reports none, as `git log` does."""
    wanted = sorted(set(commits))
    if not wanted:
        return {}
    ensure_trees(history, wanted)
    output = history.repo.run(
        "log", "--no-walk", "--stdin", "--name-only", f"--format={_RECORD_SEPARATOR}%H",
        stdin="\n".join(wanted) + "\n",
    )
    paths: Dict[str, List[str]] = {sha: [] for sha in wanted}
    for record in output.split(_RECORD_SEPARATOR):
        lines = [line for line in record.splitlines() if line.strip()]
        if lines:
            paths[lines[0].strip()] = lines[1:]
    return paths


def first_parent_commits(history: History, since: str = "", until: str = "") -> List[Dict[str, Any]]:
    """Master's own commits, newest first: the merged changes, as opposed to what they merged."""
    args = ["log", "--first-parent", f"--format={_RECORD_SEPARATOR}%H{_FIELD_SEPARATOR}%P{_FIELD_SEPARATOR}"
            f"%cI{_FIELD_SEPARATOR}%s"]
    if since:
        args.append(f"--since={since}")
    if until:
        args.append(f"--until={until}")
    args.append(history.revision)

    commits: List[Dict[str, Any]] = []
    for record in history.repo.run(*args).split(_RECORD_SEPARATOR):
        if not record.strip():
            continue
        sha, parents, committed, subject = record.rstrip("\n").split(_FIELD_SEPARATOR, 3)
        commits.append({"sha": sha, "parents": parents.split(), "committed_date": committed, "subject": subject})
    return commits
