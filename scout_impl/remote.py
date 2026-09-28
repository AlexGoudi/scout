"""Fetch a change set straight from a remote over anonymous HTTPS, with no clone.

The mechanism is a shallow, blob-filtered fetch into a cache directory keyed by remote:

    git init --bare
    git remote add origin https://github.com/sonic-net/sonic-buildimage.git
    git fetch --depth=2 --filter=blob:none origin refs/pull/<N>/head

`--filter=blob:none` brings commits and trees but defers file content, and that is the
whole point. Measured anonymously against `sonic-net/sonic-buildimage` with no
credentials: the fetch above takes under a second and leaves under a megabyte on disk, a
recursive listing of the ~15,500 paths at the fetched commit takes about 12 ms and
downloads nothing further, and the first file read costs a network round trip of roughly
half a second. Presence-and-absence questions over the tree are therefore free and
content reads are not, which is why `RepoSource` splits them (`scout_impl/source.py`).

No authentication, no `gh`, no GitHub SDK: this is `git` against a public HTTPS remote,
and `GIT_TERMINAL_PROMPT=0` makes a private or missing repository fail immediately with
a message rather than block on a credential prompt.
"""

import hashlib
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .gitcmd import CONFIG_OVERRIDES, GitError
from .source import LocalCheckout, RepoSource

logger = logging.getLogger(__name__)

CACHE_DIR_ENV = "SCOUT_CACHE_DIR"
DEFAULT_CACHE_SUBDIR = Path("sonic-scout") / "repos"

# The two halves of a range want different things from the remote. The head side is what
# gets diffed and listed, so it needs trees but only a handful of commits. The base side
# is only ever walked to find where the head branched, so it needs no trees at all — and
# that is what makes it affordable to go deep there, which a merged pull request requires
# because its base branch has moved on since. Measured on sonic-buildimage: 5,254 commits
# of master under tree:0 is 2.0 s and 5.8 MB, against roughly 0.8 s for two commits of a
# pull request under blob:none.
BLOB_FILTER = "blob:none"
COMMIT_FILTER = "tree:0"

# Two is enough for a single-commit pull request: the commit and the parent to diff it
# against. Anything longer deepens from here rather than starting deep.
DEFAULT_DEPTH = 2
MAX_DEPTH = 512
DEPTH_MULTIPLIER = 4

DEFAULT_BASE_DEPTH = 128
MAX_BASE_DEPTH = 16384
BASE_DEPTH_MULTIPLIER = 8

SCOUT_REFS = "refs/scout"
GITHUB_HOST = "github.com"

_SHORTHAND_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
_SCP_URL_RE = re.compile(r"^(?:(?P<user>[^@/]+)@)?(?P<host>[^:/]+):(?P<path>.+)$")
_MISSING_REF_RE = re.compile(r"couldn't find remote ref (\S+)")

_AUTH_MARKERS = (
    "could not read username",
    "could not read password",
    "authentication failed",
    "repository not found",
    "terminal prompts disabled",
    "403 forbidden",
)
_UNREACHABLE_MARKERS = (
    "could not resolve host",
    "failed to connect",
    "connection timed out",
    "connection refused",
    "network is unreachable",
    "operation timed out",
    "proxy connect aborted",
    "ssl connect error",
    "gnutls_handshake() failed",
    "unable to access",
)


class RemoteError(GitError):
    """A remote operation failed. Subclasses say how, so a caller can act on it."""


class RemoteUnreachable(RemoteError):
    """The remote host could not be reached at all."""


class RemoteNotReadable(RemoteError):
    """The remote exists as a URL but is not readable without credentials."""


class RefNotFound(RemoteError):
    """The remote does not publish the requested ref."""


class PullRequestNotFound(RemoteError):
    """The remote publishes no `refs/pull/<N>/head` for that number."""


class ShallowHistoryExhausted(RemoteError):
    """The change set did not fit inside the depth cap."""


@dataclass(frozen=True)
class FetchedChange:
    """What one fetch resolved to, and what it cost getting there."""

    base_sha: str
    head_sha: str
    depth: int
    base_depth: int
    fetches: int
    duration_s: float


@dataclass(frozen=True)
class _Side:
    """One half of a range fetch: what to bring, how much detail, and how far back."""

    refspecs: Tuple[str, ...]
    blob_filter: str
    depth: int
    max_depth: int
    multiplier: int

    @property
    def exhausted(self) -> bool:
        return self.depth >= self.max_depth

    def deepened(self) -> "_Side":
        return replace(self, depth=min(self.depth * self.multiplier, self.max_depth))


def normalize_remote(value: str) -> str:
    """Accept `owner/repo`, an https URL or an scp-style ssh URL; return a fetchable URL.

    The shorthand expands against GitHub, which is where every SONiC repository lives.
    """
    raw = (value or "").strip()
    if not raw:
        raise ValueError("A remote is required: owner/repo, or a URL")
    if _SHORTHAND_RE.match(raw):
        return f"https://{GITHUB_HOST}/{raw}.git"
    if "://" in raw:
        return raw

    scp = _SCP_URL_RE.match(raw)
    if scp:
        return f"https://{scp.group('host')}/{scp.group('path')}"
    raise ValueError(f"Not a remote Scout can fetch from: {value!r}")


def remote_identity(value: str) -> str:
    """`host/owner/repo`: the form on which two spellings of one remote agree.

    An ssh URL and an https URL for the same repository share a cache entry because they
    reach the same objects, and normalizing before keying is what makes that happen.
    """
    url = normalize_remote(value)
    _, _, remainder = url.partition("://")
    remainder = remainder.split("@")[-1]
    remainder = remainder.strip("/")
    if remainder.endswith(".git"):
        remainder = remainder[: -len(".git")]
    return remainder.lower()


def default_cache_root() -> Path:
    """Where fetched repositories are cached: `$SCOUT_CACHE_DIR`, else the XDG cache."""
    configured = os.environ.get(CACHE_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / DEFAULT_CACHE_SUBDIR


def cache_key(value: str) -> str:
    """Directory name for a remote: a readable slug plus a digest of its identity.

    The slug is for a human reading `ls` on the cache; the digest is what guarantees two
    different remotes never collide after the slug flattens their separators.
    """
    identity = remote_identity(value)
    slug = re.sub(r"[^a-z0-9]+", "-", identity).strip("-")[:64]
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}"


def pull_request_refs(number: int) -> Tuple[str, str]:
    """The two refs GitHub publishes for a pull request: its head, and its test merge."""
    if int(number) <= 0:
        raise ValueError(f"Not a pull request number: {number!r}")
    return f"refs/pull/{int(number)}/head", f"refs/pull/{int(number)}/merge"


def fetch_args(depth: int, refspecs: Sequence[str], blob_filter: str = BLOB_FILTER) -> List[str]:
    """Build the fetch command. Kept pure so the flags are testable without a network."""
    if depth < 1:
        raise ValueError(f"Fetch depth must be at least 1, got {depth}")
    if not refspecs:
        raise ValueError("A fetch needs at least one refspec")

    args = ["fetch", "--quiet", "--no-tags", f"--depth={depth}"]
    if blob_filter:
        args.append(f"--filter={blob_filter}")
    args.append("origin")
    args.extend(refspecs)
    return args


def classify_remote_error(stderr: str, url: str, refs: Sequence[str] = ()) -> RemoteError:
    """Turn git's stderr into an error a reader can act on.

    Three failures have to stay distinguishable — an unreachable network, a pull request
    that does not exist, and a ref that does not exist — and git reports the last two
    with the same sentence, so which ref was asked for is what separates them.
    """
    text = (stderr or "").strip()
    lowered = text.lower()

    missing = _MISSING_REF_RE.search(text)
    if missing:
        ref = missing.group(1)
        pull_number = re.match(r"refs/pull/(\d+)/head$", ref)
        if pull_number:
            return PullRequestNotFound(
                f"{url} has no pull request {pull_number.group(1)}: the remote does not publish {ref}. "
                f"Check the number, and that the pull request belongs to this repository."
            )
        return RefNotFound(
            f"{url} has no ref {ref!r}. Run `git ls-remote {url}` to see what it does publish."
        )

    if any(marker in lowered for marker in _AUTH_MARKERS):
        return RemoteNotReadable(
            f"{url} is not readable anonymously, so it is private or does not exist. "
            f"Scout fetches without credentials by design and will not authenticate. git said: {text}"
        )

    if any(marker in lowered for marker in _UNREACHABLE_MARKERS):
        return RemoteUnreachable(
            f"Cannot reach {url}. Scout fetches over anonymous HTTPS, so check network access to that host "
            f"and HTTPS_PROXY if you are behind a proxy. git said: {text}"
        )

    wanted = f" while fetching {', '.join(refs)}" if refs else ""
    return RemoteError(f"git failed against {url}{wanted}: {text}")


class RemoteRepo(RepoSource):
    """A repository read straight from a remote, cached as a bare partial clone.

    Bare because the cache is never checked out: git refuses working-tree operations
    against it, which is the guardrail that keeps a read-only cache read-only.
    """

    def __init__(
        self,
        url: str,
        cache_root: Optional[Path] = None,
        depth: int = DEFAULT_DEPTH,
        max_depth: int = MAX_DEPTH,
        base_depth: int = DEFAULT_BASE_DEPTH,
        max_base_depth: int = MAX_BASE_DEPTH,
        timeout_seconds: int = 300,
    ) -> None:
        super().__init__()
        self.url = normalize_remote(url)
        self.identity = remote_identity(self.url)
        self.cache_root = Path(cache_root) if cache_root is not None else default_cache_root()
        self.cache_dir = self.cache_root / cache_key(self.url)
        # `depth` bounds the commits that get diffed; `base_depth` bounds the walk back to
        # the branch point, which starts deeper because commit-graph objects are tiny.
        self.depth = depth
        self.max_depth = max_depth
        self.base_depth = base_depth
        self.max_base_depth = max_base_depth
        self.timeout_seconds = timeout_seconds
        self._initialized = False
        self._fetched: Set[Tuple[str, ...]] = set()

    @property
    def describe(self) -> str:
        return f"{self.url} (cached in {self.cache_dir})"

    def git(self, *args: str, check: bool = True, stdin: Optional[str] = None) -> str:
        self._ensure_cache()
        returncode, stdout, stderr = self._run(list(args), stdin=stdin)
        if check and returncode != 0:
            raise classify_remote_error(stderr, self.url)
        return stdout

    def cache_size_bytes(self) -> int:
        """How much disk this remote's cache occupies."""
        if not self.cache_dir.is_dir():
            return 0
        return sum(path.stat().st_size for path in self.cache_dir.rglob("*") if path.is_file())

    def prefetch_blobs(self, shas: Sequence[str]) -> int:
        """Fetch many missing blobs in **one** round trip instead of one trip each.

        This is the same mechanism git's own lazy fetch uses when `cat-file` meets a blob a
        `--filter=blob:none` clone does not have — `fetch --stdin` with the object ids on
        standard input — with the one difference that matters: git issues it per object, and
        this issues it once for the whole set. Reading every symlink target under `device/`
        is 352 distinct blobs, which is 352 round trips and about three minutes at the
        measured rate, against one trip and a couple of seconds here.

        `fetch.negotiationAlgorithm=noop` skips the have/want negotiation, which is pure
        overhead when the wants are object ids rather than refs. Failure is not an error:
        the caller falls back to reading one at a time, which is slower and identical.
        """
        wanted = [sha for sha in dict.fromkeys(shas) if sha]
        if not wanted:
            return 0

        started = time.monotonic()
        returncode, _, stderr = self._run(
            [
                "-c", "fetch.negotiationAlgorithm=noop",
                "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                "--recurse-submodules=no", f"--filter={BLOB_FILTER}", "--stdin", "origin",
            ],
            stdin="\n".join(wanted) + "\n",
        )
        if returncode != 0:
            logger.info(
                "Batched prefetch of %d blob(s) from %s did not work, falling back to one read at a "
                "time; git said: %s",
                len(wanted), self.url, (stderr or "").strip().splitlines()[-1:] or "nothing",
            )
            return 0

        logger.info("Prefetched %d blob(s) from %s in one round trip, %.2fs",
                    len(wanted), self.url, time.monotonic() - started)
        return len(wanted)

    def ls_remote(self, *patterns: str) -> Dict[str, str]:
        """Ask the remote which of these refs exist, without transferring any object.

        One round trip, and the cheapest way to tell "no such pull request" apart from
        "the fetch failed", because it happens before any object negotiation.
        """
        self._ensure_cache()
        returncode, stdout, stderr = self._run(["ls-remote", self.url, *patterns])
        if returncode != 0:
            raise classify_remote_error(stderr, self.url, patterns)

        refs: Dict[str, str] = {}
        for line in stdout.splitlines():
            sha, _, ref = line.partition("\t")
            if ref:
                refs[ref.strip()] = sha.strip()
        return refs

    def default_branch(self) -> str:
        """The remote's own HEAD, used as the base when a pull request has no merge ref."""
        self._ensure_cache()
        returncode, stdout, stderr = self._run(["ls-remote", "--symref", self.url, "HEAD"])
        if returncode != 0:
            raise classify_remote_error(stderr, self.url, ("HEAD",))
        for line in stdout.splitlines():
            if line.startswith("ref: "):
                return line[len("ref: "):].split("\t")[0].strip()
        raise RemoteError(f"{self.url} did not report a default branch")

    def fetch_pull_request(self, number: int, base_ref: Optional[str] = None) -> FetchedChange:
        """Fetch pull request `number` and resolve the range it represents.

        GitHub publishes `refs/pull/<N>/head` for every pull request and, for those it
        has test-merged, `refs/pull/<N>/merge`, whose first parent is the base commit.
        When the merge ref is there the base comes for free and the base branch is never
        fetched at all. When it is not — a merged or conflicting pull request — the base
        falls back to `base_ref`, or to the remote's default branch.
        """
        head_ref, merge_ref = pull_request_refs(number)
        published = self.ls_remote(head_ref, merge_ref)
        if head_ref not in published:
            raise PullRequestNotFound(
                f"{self.url} has no pull request {number}: the remote does not publish {head_ref}. "
                f"Check the number, and that the pull request belongs to this repository."
            )

        local_head = f"{SCOUT_REFS}/pull/{number}/head"
        head_side = self._head_side([f"+{head_ref}:{local_head}"])

        if merge_ref in published:
            local_merge = f"{SCOUT_REFS}/pull/{number}/merge"
            base_side = self._base_side([f"+{merge_ref}:{local_merge}"])
            base_rev = f"{local_merge}^1"
        else:
            branch = base_ref or self.default_branch()
            local_base = f"{SCOUT_REFS}/base/{branch.rsplit('/', 1)[-1]}"
            base_side = self._base_side([f"+{branch}:{local_base}"])
            base_rev = local_base
            logger.info(
                "Pull request %d has no merge ref, so its base comes from %s; this is normal once a "
                "pull request is merged or has conflicts, and means walking further back to the branch point",
                number,
                branch,
            )

        return self._resolve_range(
            head_side,
            base_side,
            base_rev,
            local_head,
            use_merge_base=True,
            expected_head=published[head_ref],
        )

    def fetch_range(self, base_ref: str, head_ref: str, merge_base: bool = True) -> FetchedChange:
        """Fetch two refs and resolve the range between them.

        `merge_base` mirrors git's three-dot range: the base becomes the fork point
        rather than the named ref. Either ref may be a branch, a tag or a raw sha.
        """
        local_base = f"{SCOUT_REFS}/range/base"
        local_head = f"{SCOUT_REFS}/range/head"
        return self._resolve_range(
            self._head_side([f"+{head_ref}:{local_head}"]),
            self._base_side([f"+{base_ref}:{local_base}"]),
            local_base,
            local_head,
            use_merge_base=merge_base,
        )

    def _head_side(self, refspecs: Sequence[str]) -> _Side:
        return _Side(tuple(refspecs), BLOB_FILTER, self.depth, self.max_depth, DEPTH_MULTIPLIER)

    def _base_side(self, refspecs: Sequence[str]) -> _Side:
        return _Side(tuple(refspecs), COMMIT_FILTER, self.base_depth, self.max_base_depth, BASE_DEPTH_MULTIPLIER)

    def _resolve_range(
        self,
        head_side: _Side,
        base_side: _Side,
        base_rev: str,
        head_rev: str,
        use_merge_base: bool,
        expected_head: Optional[str] = None,
    ) -> FetchedChange:
        """Fetch both sides, then deepen until the range is whole, or say why it is not.

        Depth multiplies rather than steps, because the common case fits at the start and
        the uncommon one should not cost a dozen round trips. Whole history is never
        fetched: both sides stop at their cap. The base side is fetched first so the
        remote's recorded filter ends up as `blob:none`, which is what any later lazy
        read of a file should use.
        """
        started = time.monotonic()
        fetches = 0

        cached = self._resolve_from_cache(base_rev, head_rev, use_merge_base, expected_head)
        if cached:
            logger.info("Serving %s..%s for %s from the cache", cached[0][:9], cached[1][:9], self.url)
            return FetchedChange(
                base_sha=cached[0],
                head_sha=cached[1],
                depth=0,
                base_depth=0,
                fetches=0,
                duration_s=time.monotonic() - started,
            )

        while True:
            self._fetch(base_side)
            self._fetch(head_side)
            fetches += 1

            head_sha = self.rev_parse(head_rev)
            base_sha = self._rev_parse_optional(base_rev)
            resolved = self._resolved_base(base_sha, head_sha, use_merge_base) if base_sha else None
            if resolved:
                duration = time.monotonic() - started
                logger.info(
                    "Fetched %s..%s from %s at depth %d/%d in %d round(s), %.2fs",
                    resolved[:9],
                    head_sha[:9],
                    self.url,
                    head_side.depth,
                    base_side.depth,
                    fetches,
                    duration,
                )
                return FetchedChange(
                    base_sha=resolved,
                    head_sha=head_sha,
                    depth=head_side.depth,
                    base_depth=base_side.depth,
                    fetches=fetches,
                    duration_s=duration,
                )

            if head_side.exhausted and base_side.exhausted:
                raise ShallowHistoryExhausted(
                    f"Could not connect {base_rev} to {head_rev} on {self.url} within depth "
                    f"{head_side.max_depth} of the head and {base_side.max_depth} of the base. Either the "
                    f"branch point is further back than that, in which case raise max_depth and "
                    f"max_base_depth, or the two refs share no history at all."
                )
            head_side = head_side.deepened()
            base_side = base_side.deepened()
            logger.info("Range is not whole yet; deepening to %d/%d", head_side.depth, base_side.depth)

    def _resolve_from_cache(
        self,
        base_rev: str,
        head_rev: str,
        use_merge_base: bool,
        expected_head: Optional[str],
    ) -> Optional[Tuple[str, str]]:
        """Answer from what a previous run already fetched, or None to go to the network.

        Worth doing for more than the saved transfer. `git fetch --depth` shortens a
        history as readily as it deepens one, so re-fetching at the starting depth into
        a cache a previous run had already deepened would throw that work away and pay
        for it again on the way back down.
        """
        if expected_head is None:
            return None

        head_sha = self._rev_parse_optional(head_rev)
        if head_sha != expected_head:
            return None

        base_sha = self._rev_parse_optional(base_rev)
        if not base_sha:
            return None

        resolved = self._resolved_base(base_sha, head_sha, use_merge_base)
        return (resolved, head_sha) if resolved else None

    def _resolved_base(self, base_sha: str, head_sha: str, use_merge_base: bool) -> Optional[str]:
        """The base to diff from, or None when more history is needed to know it."""
        boundary = self._shallow_boundary()

        if use_merge_base:
            merge_base = self.git("merge-base", base_sha, head_sha, check=False).strip()
            # A shallow boundary commit looks like a common ancestor because its own
            # parents are missing, so it is never accepted as the real fork point.
            if not merge_base or merge_base in boundary:
                return None
            base_sha = merge_base

        if boundary and set(self._rev_list(base_sha, head_sha)) & boundary:
            return None
        return base_sha

    def _rev_list(self, base_sha: str, head_sha: str) -> List[str]:
        return self.git("rev-list", f"{base_sha}..{head_sha}", check=False).split()

    def _shallow_boundary(self) -> Set[str]:
        """Commits whose parents were not fetched; a walk reaching one was truncated.

        git records these in the `shallow` file of the repository, which is part of the
        on-disk layout and has no porcelain equivalent.
        """
        shallow_file = self.cache_dir / "shallow"
        if not shallow_file.is_file():
            return set()
        return set(shallow_file.read_text(encoding="utf-8").split())

    def _rev_parse_optional(self, revision: str) -> Optional[str]:
        resolved = self.git("rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}", check=False).strip()
        return resolved or None

    def _fetch(self, side: _Side) -> None:
        memo = (str(side.depth), side.blob_filter, *side.refspecs)
        if memo in self._fetched:
            return

        self._ensure_cache()
        args = fetch_args(side.depth, side.refspecs, side.blob_filter)
        returncode, _, stderr = self._run(args)
        if returncode != 0:
            raise classify_remote_error(stderr, self.url, side.refspecs)
        self._fetched.add(memo)

    def _ensure_cache(self) -> None:
        """Create the cache for this remote once, and reuse it on every later run.

        Reuse is the point: git negotiates against the objects already here, so a second
        run against the same remote transfers only what changed.
        """
        if self._initialized:
            return

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if (self.cache_dir / "HEAD").is_file():
            logger.debug("Reusing cached repository for %s in %s", self.url, self.cache_dir)
        else:
            logger.info("Creating cache for %s in %s", self.url, self.cache_dir)
            for args in (["init", "--bare", "--quiet"], ["remote", "add", "origin", self.url]):
                returncode, _, stderr = self._run(args)
                if returncode != 0:
                    raise RemoteError(f"Could not prepare the cache in {self.cache_dir}: {stderr.strip()}")
        self._initialized = True

    def _run(self, args: Sequence[str], stdin: Optional[str] = None) -> Tuple[int, str, str]:
        """Run one git command in the cache. The single seam every other method goes through."""
        environment = dict(os.environ)
        # Without this a private or missing repository blocks on a credential prompt
        # instead of failing with a message Scout can classify.
        environment["GIT_TERMINAL_PROMPT"] = "0"

        logger.debug("Running git %s in %s", " ".join(args), self.cache_dir)
        completed = subprocess.run(
            ["git", *CONFIG_OVERRIDES, *args],
            cwd=str(self.cache_dir),
            input=stdin,
            capture_output=True,
            text=True,
            errors="replace",
            env=environment,
            timeout=self.timeout_seconds,
        )
        return completed.returncode, completed.stdout, completed.stderr


def open_source(
    location: str,
    cache_root: Optional[Path] = None,
    depth: int = DEFAULT_DEPTH,
    max_depth: int = MAX_DEPTH,
) -> RepoSource:
    """Open a checkout on disk or a remote, whichever `location` names."""
    candidate = Path(location).expanduser()
    if candidate.is_dir():
        return LocalCheckout(candidate)
    return RemoteRepo(location, cache_root=cache_root, depth=depth, max_depth=max_depth)
