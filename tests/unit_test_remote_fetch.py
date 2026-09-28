"""The remote fetcher, with git replaced by a script so nothing here touches a network.

What is proved offline: the URLs and cache keys, the exact fetch flags, the refs a pull
request resolves through, that a cache is created once per remote and reused, that the
deepening loop terminates and stops at its cap, and that each failure produces its own
error class. What cannot be proved offline — that the real fetch is cheap and that a
tree listing downloads nothing — is in `tests/integration_test_remote_fetch.py`.

The stderr strings the error tests assert against are the ones git 2.34 actually emits,
captured from `sonic-net/sonic-buildimage` while building this.
"""

from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pytest

from scout_impl.remote import (
    BLOB_FILTER,
    CACHE_DIR_ENV,
    COMMIT_FILTER,
    DEFAULT_BASE_DEPTH,
    DEFAULT_DEPTH,
    PullRequestNotFound,
    RefNotFound,
    RemoteError,
    RemoteNotReadable,
    RemoteRepo,
    RemoteUnreachable,
    ShallowHistoryExhausted,
    cache_key,
    classify_remote_error,
    default_cache_root,
    fetch_args,
    normalize_remote,
    open_source,
    pull_request_refs,
    remote_identity,
)
from scout_impl.source import LocalCheckout

BUILDIMAGE = "sonic-net/sonic-buildimage"
BUILDIMAGE_URL = "https://github.com/sonic-net/sonic-buildimage.git"

HEAD_SHA = "12c9bf132ad19f52b4607347d996c38ee55012f3"
BASE_SHA = "97593ccb44900472cec83f7ea1f546cb331c5f3a"
FORK_POINT = "e04546a9f4646a4d6d26dff7627ce3b8f2c5684d"
BOUNDARY_SHA = "05c6488029eea267dbca45fba32e5b0f0a987b97"

# Verbatim from git 2.34 against the real remote; see the module docstring.
STDERR_MISSING_PR = "fatal: couldn't find remote ref refs/pull/99999999/head"
STDERR_MISSING_REF = "fatal: couldn't find remote ref refs/heads/no-such-branch"
STDERR_UNREACHABLE = "fatal: unable to access 'https://nope.invalid/x/y.git/': Proxy CONNECT aborted"
STDERR_NO_DNS = "fatal: unable to access 'https://nope.invalid/': Could not resolve host: nope.invalid"
STDERR_PRIVATE = "fatal: could not read Username for 'https://github.com': No such device or address"


class ScriptedRemote(RemoteRepo):
    """A `RemoteRepo` with its single git seam replaced by a script.

    Models just enough of a shallow repository for the deepening loop to be real: refs
    resolve, and the shallow boundary disappears once the fetch is deep enough, which is
    exactly the signal `_resolved_base` reads.
    """

    def __init__(
        self,
        url: str = BUILDIMAGE,
        published: Optional[Dict[str, str]] = None,
        complete_at_depth: int = DEFAULT_DEPTH,
        failures: Optional[Dict[str, Tuple[int, str]]] = None,
        default_branch_ref: str = "refs/heads/master",
        cached_refs: Sequence[str] = (),
        **kwargs: object,
    ) -> None:
        self.calls: List[List[str]] = []
        self.published = {"refs/pull/42/head": HEAD_SHA} if published is None else published
        # Local refs only exist once something has fetched them, which is what lets the
        # cache fast path be told apart from a fetch.
        self.local_refs = set(cached_refs)
        # The head side is what has to reach back over the pull request's own commits,
        # so it is what decides whether the range is whole.
        self.complete_at_depth = complete_at_depth
        self.failures = failures or {}
        self.default_branch_ref = default_branch_ref
        self.head_depth = 0
        self.deepest_fetch = 0
        super().__init__(url, **kwargs)

    @property
    def whole(self) -> bool:
        return self.head_depth >= self.complete_at_depth

    @property
    def fetch_calls(self) -> List[List[str]]:
        return [call for call in self.calls if call and call[0] == "fetch"]

    def refspecs_of(self, call: List[str]) -> List[str]:
        return call[call.index("origin") + 1:]

    def fetches_filtered(self, blob_filter: str) -> List[List[str]]:
        return [call for call in self.fetch_calls if f"--filter={blob_filter}" in call]

    def depths_filtered(self, blob_filter: str) -> List[int]:
        return [
            int(arg.split("=", 1)[1])
            for call in self.fetches_filtered(blob_filter)
            for arg in call
            if arg.startswith("--depth=")
        ]

    def _run(self, args: Sequence[str], stdin: Optional[str] = None) -> Tuple[int, str, str]:
        args = list(args)
        self.calls.append(args)
        command = args[0]

        if command in self.failures:
            returncode, stderr = self.failures[command]
            return returncode, "", stderr
        if command in ("init", "remote"):
            return 0, "", ""
        if command == "ls-remote":
            return 0, self._handle_ls_remote(args), ""
        if command == "fetch":
            return 0, "", self._handle_fetch(args)
        if command == "rev-parse":
            return 0, f"{self._handle_resolve(args[-1])}\n", ""
        if command == "merge-base":
            return 0, f"{FORK_POINT}\n", ""
        if command == "rev-list":
            return 0, self._handle_rev_list(), ""
        return 0, "", ""

    def _handle_ls_remote(self, args: List[str]) -> str:
        if "--symref" in args:
            return f"ref: {self.default_branch_ref}\tHEAD\n{BASE_SHA}\tHEAD\n"
        patterns = args[2:]
        return "".join(f"{sha}\t{ref}\n" for ref, sha in self.published.items() if ref in patterns)

    def _handle_fetch(self, args: List[str]) -> str:
        depth = next(int(arg.split("=", 1)[1]) for arg in args if arg.startswith("--depth="))
        self.deepest_fetch = max(self.deepest_fetch, depth)
        if f"--filter={BLOB_FILTER}" in args:
            self.head_depth = max(self.head_depth, depth)
        self.local_refs.update(refspec.partition(":")[2] for refspec in self.refspecs_of(args))

        shallow = self.cache_dir / "shallow"
        if self.whole:
            shallow.unlink(missing_ok=True)
        else:
            shallow.write_text(f"{BOUNDARY_SHA}\n{BASE_SHA}\n", encoding="utf-8")
        return ""

    def _handle_rev_list(self) -> str:
        return f"{HEAD_SHA}\n" if self.whole else f"{HEAD_SHA}\n{BOUNDARY_SHA}\n"

    def _handle_resolve(self, revision: str) -> str:
        ref = revision.replace("^{commit}", "").rstrip("^12")
        if ref not in self.local_refs:
            return ""
        base_side = revision.startswith(BASE_SHA) or "merge^1" in revision or "base" in revision
        return BASE_SHA if base_side else HEAD_SHA


@pytest.fixture
def cache_root(tmp_path: Path) -> Path:
    return tmp_path / "cache"


# --- URLs, identity and cache keys --------------------------------------------------

@pytest.mark.parametrize(
    "value",
    [
        BUILDIMAGE,
        BUILDIMAGE_URL,
        "https://github.com/sonic-net/sonic-buildimage",
        "git@github.com:sonic-net/sonic-buildimage.git",
        "ssh://git@github.com/sonic-net/sonic-buildimage.git",
    ],
)
def test_every_spelling_of_one_remote_shares_an_identity_and_a_cache_entry(value: str) -> None:
    assert remote_identity(value) == "github.com/sonic-net/sonic-buildimage"
    assert cache_key(value) == cache_key(BUILDIMAGE)


def test_the_shorthand_expands_against_github() -> None:
    assert normalize_remote(BUILDIMAGE) == BUILDIMAGE_URL
    assert normalize_remote("https://gitlab.example.com/x/y.git") == "https://gitlab.example.com/x/y.git"


def test_a_remote_scout_cannot_fetch_from_is_rejected() -> None:
    for bad in ("", "   ", "not a remote at all"):
        with pytest.raises(ValueError):
            normalize_remote(bad)


def test_different_remotes_never_share_a_cache_entry() -> None:
    assert cache_key(BUILDIMAGE) != cache_key("sonic-net/sonic-mgmt")
    assert cache_key("https://github.com/a/b.git") != cache_key("https://gitlab.com/a/b.git")


def test_a_cache_key_is_readable_before_it_is_unique() -> None:
    key = cache_key(BUILDIMAGE)
    assert key.startswith("github-com-sonic-net-sonic-buildimage-")
    assert len(key.rsplit("-", 1)[1]) == 12


def test_the_cache_root_is_configurable_and_otherwise_lands_in_the_xdg_cache(monkeypatch) -> None:
    monkeypatch.setenv(CACHE_DIR_ENV, "/tmp/scout-cache-somewhere")
    assert default_cache_root() == Path("/tmp/scout-cache-somewhere")

    monkeypatch.delenv(CACHE_DIR_ENV)
    monkeypatch.setenv("XDG_CACHE_HOME", "/tmp/xdg")
    assert default_cache_root() == Path("/tmp/xdg/sonic-scout/repos")


def test_a_remote_is_cached_under_its_own_key(cache_root: Path) -> None:
    remote = RemoteRepo(BUILDIMAGE, cache_root=cache_root)
    assert remote.cache_dir.parent == cache_root
    assert remote.cache_dir.name == cache_key(BUILDIMAGE)
    assert RemoteRepo("sonic-net/sonic-mgmt", cache_root=cache_root).cache_dir != remote.cache_dir


# --- command construction -----------------------------------------------------------

def test_the_fetch_is_shallow_and_blob_filtered() -> None:
    args = fetch_args(2, ["+refs/pull/42/head:refs/scout/pull/42/head"])
    assert args == [
        "fetch",
        "--quiet",
        "--no-tags",
        "--depth=2",
        f"--filter={BLOB_FILTER}",
        "origin",
        "+refs/pull/42/head:refs/scout/pull/42/head",
    ]


def test_the_base_side_asks_for_commits_only() -> None:
    args = fetch_args(128, ["+refs/heads/master:refs/scout/base/master"], COMMIT_FILTER)
    assert f"--filter={COMMIT_FILTER}" in args
    assert "--depth=128" in args


def test_the_blob_filter_is_what_makes_the_fetch_cheap_and_is_not_optional_by_accident() -> None:
    assert f"--filter={BLOB_FILTER}" in fetch_args(2, ["+a:b"])
    assert not [arg for arg in fetch_args(2, ["+a:b"], blob_filter="") if arg.startswith("--filter")]


def test_a_nonsense_fetch_is_refused_before_git_sees_it() -> None:
    with pytest.raises(ValueError):
        fetch_args(0, ["+a:b"])
    with pytest.raises(ValueError):
        fetch_args(2, [])


def test_a_pull_request_names_the_two_refs_github_publishes() -> None:
    assert pull_request_refs(23052) == ("refs/pull/23052/head", "refs/pull/23052/merge")
    with pytest.raises(ValueError):
        pull_request_refs(0)


# --- fetching a pull request ---------------------------------------------------------

def test_a_pull_request_with_a_merge_ref_gets_its_base_without_fetching_a_branch(cache_root: Path) -> None:
    remote = ScriptedRemote(
        published={"refs/pull/42/head": HEAD_SHA, "refs/pull/42/merge": "deadbeef" * 5},
        cache_root=cache_root,
    )
    fetched = remote.fetch_pull_request(42)

    assert fetched.base_sha == FORK_POINT
    assert fetched.head_sha == HEAD_SHA
    assert fetched.depth == DEFAULT_DEPTH
    assert fetched.base_depth == DEFAULT_BASE_DEPTH
    assert fetched.fetches == 1

    assert remote.refspecs_of(remote.fetches_filtered(BLOB_FILTER)[0]) == [
        "+refs/pull/42/head:refs/scout/pull/42/head",
    ]
    # The merge ref is the base side: only its commit graph is walked, to find the fork
    # point, so it never needs the trees the head side does.
    assert remote.refspecs_of(remote.fetches_filtered(COMMIT_FILTER)[0]) == [
        "+refs/pull/42/merge:refs/scout/pull/42/merge",
    ]
    assert not any("refs/heads/master" in " ".join(call) for call in remote.fetch_calls)


def test_the_base_side_is_fetched_before_the_head_side(cache_root: Path) -> None:
    # Order decides which filter the remote records, and a later lazy read of a file
    # should use blob:none rather than tree:0.
    remote = ScriptedRemote(cache_root=cache_root)
    remote.fetch_pull_request(42)

    filters = [arg for call in remote.fetch_calls for arg in call if arg.startswith("--filter=")]
    assert filters[:2] == [f"--filter={COMMIT_FILTER}", f"--filter={BLOB_FILTER}"]


def test_a_merged_pull_request_falls_back_to_the_default_branch(cache_root: Path) -> None:
    remote = ScriptedRemote(published={"refs/pull/42/head": HEAD_SHA}, cache_root=cache_root)
    fetched = remote.fetch_pull_request(42)

    assert fetched.base_sha == FORK_POINT
    assert remote.refspecs_of(remote.fetches_filtered(COMMIT_FILTER)[0]) == [
        "+refs/heads/master:refs/scout/base/master",
    ]


def test_an_explicit_base_ref_overrides_the_default_branch(cache_root: Path) -> None:
    remote = ScriptedRemote(published={"refs/pull/42/head": HEAD_SHA}, cache_root=cache_root)
    remote.fetch_pull_request(42, base_ref="refs/heads/202405")

    assert "+refs/heads/202405:refs/scout/base/202405" in remote.fetches_filtered(COMMIT_FILTER)[0]
    assert not any("--symref" in call for call in remote.calls)


def test_a_pull_request_that_does_not_exist_is_caught_before_any_fetch(cache_root: Path) -> None:
    remote = ScriptedRemote(published={}, cache_root=cache_root)

    with pytest.raises(PullRequestNotFound) as error:
        remote.fetch_pull_request(99999999)
    assert "99999999" in str(error.value)
    assert remote.fetch_calls == []


# --- depth and deepening --------------------------------------------------------------

def test_a_single_commit_pull_request_is_done_at_depth_two(cache_root: Path) -> None:
    remote = ScriptedRemote(complete_at_depth=2, cache_root=cache_root)
    fetched = remote.fetch_pull_request(42)

    assert fetched.depth == 2
    assert fetched.fetches == 1


def test_a_longer_pull_request_deepens_instead_of_fetching_whole_history(cache_root: Path) -> None:
    remote = ScriptedRemote(complete_at_depth=32, cache_root=cache_root)
    fetched = remote.fetch_pull_request(42)

    # The two sides deepen on their own schedules: the head a little, the base a lot,
    # because only one of them pays for trees.
    assert remote.depths_filtered(BLOB_FILTER) == [2, 8, 32]
    assert remote.depths_filtered(COMMIT_FILTER) == [128, 1024, 8192]
    assert fetched.depth == 32
    assert fetched.base_depth == 8192
    assert fetched.fetches == 3


def test_deepening_stops_at_the_cap_and_says_how_to_raise_it(cache_root: Path) -> None:
    remote = ScriptedRemote(complete_at_depth=10 ** 9, cache_root=cache_root, max_depth=32, max_base_depth=256)

    with pytest.raises(ShallowHistoryExhausted) as error:
        remote.fetch_pull_request(42)
    assert "32" in str(error.value) and "256" in str(error.value)
    assert "max_depth" in str(error.value)
    assert "share no history" in str(error.value)
    assert remote.deepest_fetch == 256


def test_an_arbitrary_range_uses_the_same_deepening(cache_root: Path) -> None:
    remote = ScriptedRemote(complete_at_depth=2, cache_root=cache_root)
    fetched = remote.fetch_range("refs/heads/master", "refs/heads/feature")

    assert fetched.head_sha == HEAD_SHA
    assert remote.refspecs_of(remote.fetches_filtered(BLOB_FILTER)[0]) == [
        "+refs/heads/feature:refs/scout/range/head",
    ]
    assert remote.refspecs_of(remote.fetches_filtered(COMMIT_FILTER)[0]) == [
        "+refs/heads/master:refs/scout/range/base",
    ]


def test_a_two_dot_range_keeps_the_named_base_rather_than_the_fork_point(cache_root: Path) -> None:
    remote = ScriptedRemote(complete_at_depth=2, cache_root=cache_root)
    fetched = remote.fetch_range(BASE_SHA, HEAD_SHA, merge_base=False)

    assert fetched.base_sha == BASE_SHA
    assert not any(call[0] == "merge-base" for call in remote.calls)


# --- caching ---------------------------------------------------------------------------

def test_the_cache_is_created_once_and_reused_by_the_next_run(cache_root: Path) -> None:
    first = ScriptedRemote(cache_root=cache_root)
    first.fetch_pull_request(42)
    assert [call for call in first.calls if call[0] == "init"]
    assert (first.cache_dir).is_dir()

    # A later run against the same remote finds the cache and does not re-create it,
    # which is what lets git transfer only what is missing.
    (first.cache_dir / "HEAD").write_text("ref: refs/heads/master\n", encoding="utf-8")
    second = ScriptedRemote(cache_root=cache_root)
    second.fetch_pull_request(42)

    assert second.cache_dir == first.cache_dir
    assert [call for call in second.calls if call[0] == "init"] == []


def test_refetching_the_same_refs_in_one_run_costs_nothing(cache_root: Path) -> None:
    remote = ScriptedRemote(complete_at_depth=2, cache_root=cache_root)
    remote.fetch_pull_request(42)
    fetches_after_first = len(remote.fetch_calls)
    remote.fetch_pull_request(42)

    assert len(remote.fetch_calls) == fetches_after_first


def test_a_cache_that_already_holds_the_range_is_not_refetched(cache_root: Path) -> None:
    """Not only to save the transfer: `--depth` shortens history as readily as it
    deepens it, so refetching at the starting depth would undo a previous run's work."""
    remote = ScriptedRemote(
        published={"refs/pull/42/head": HEAD_SHA},
        cache_root=cache_root,
        cached_refs=("refs/scout/pull/42/head", "refs/scout/base/master"),
    )
    fetched = remote.fetch_pull_request(42)

    assert fetched.fetches == 0
    assert (fetched.base_sha, fetched.head_sha) == (FORK_POINT, HEAD_SHA)
    assert remote.fetch_calls == []


def test_a_force_pushed_pull_request_is_refetched_rather_than_served_stale(cache_root: Path) -> None:
    remote = ScriptedRemote(
        published={"refs/pull/42/head": "f0f0f0f0" * 5},
        cache_root=cache_root,
        cached_refs=("refs/scout/pull/42/head", "refs/scout/base/master"),
    )
    remote.fetch_pull_request(42)

    assert remote.fetch_calls, "the cached head no longer matches the remote, so it must be refetched"


def test_cache_size_is_reportable(cache_root: Path) -> None:
    remote = ScriptedRemote(cache_root=cache_root)
    assert remote.cache_size_bytes() == 0
    remote.fetch_pull_request(42)
    (remote.cache_dir / "objects").mkdir(parents=True, exist_ok=True)
    (remote.cache_dir / "objects" / "pack").write_bytes(b"x" * 1024)
    assert remote.cache_size_bytes() >= 1024


# --- errors -------------------------------------------------------------------------

def test_a_missing_pull_request_and_a_missing_ref_stay_distinguishable() -> None:
    pull_error = classify_remote_error(STDERR_MISSING_PR, BUILDIMAGE_URL)
    ref_error = classify_remote_error(STDERR_MISSING_REF, BUILDIMAGE_URL)

    assert isinstance(pull_error, PullRequestNotFound)
    assert "99999999" in str(pull_error)
    assert isinstance(ref_error, RefNotFound)
    assert not isinstance(ref_error, PullRequestNotFound)
    assert "no-such-branch" in str(ref_error)
    assert "git ls-remote" in str(ref_error)


@pytest.mark.parametrize("stderr", [STDERR_UNREACHABLE, STDERR_NO_DNS])
def test_an_unreachable_network_says_so_and_mentions_the_proxy(stderr: str) -> None:
    error = classify_remote_error(stderr, BUILDIMAGE_URL)

    assert isinstance(error, RemoteUnreachable)
    assert "Cannot reach" in str(error)
    assert "HTTPS_PROXY" in str(error)


def test_a_private_or_missing_repository_is_not_reported_as_a_network_failure() -> None:
    error = classify_remote_error(STDERR_PRIVATE, BUILDIMAGE_URL)

    assert isinstance(error, RemoteNotReadable)
    assert not isinstance(error, RemoteUnreachable)
    assert "will not authenticate" in str(error)


def test_an_unrecognized_failure_still_names_the_remote_and_the_refs() -> None:
    error = classify_remote_error("fatal: the pack is corrupt", BUILDIMAGE_URL, ("refs/pull/1/head",))

    assert type(error) is RemoteError
    assert BUILDIMAGE_URL in str(error)
    assert "refs/pull/1/head" in str(error)


def test_every_remote_error_is_a_git_error_so_existing_handlers_still_catch_it() -> None:
    from scout_impl.gitcmd import GitError

    for error_type in (RemoteError, RemoteUnreachable, RemoteNotReadable, RefNotFound,
                       PullRequestNotFound, ShallowHistoryExhausted):
        assert issubclass(error_type, GitError)


def test_a_failing_fetch_is_classified_rather_than_raised_raw(cache_root: Path) -> None:
    remote = ScriptedRemote(cache_root=cache_root, failures={"fetch": (128, STDERR_UNREACHABLE)})

    with pytest.raises(RemoteUnreachable):
        remote.fetch_pull_request(42)


def test_a_failing_ls_remote_is_classified_too(cache_root: Path) -> None:
    remote = ScriptedRemote(cache_root=cache_root, failures={"ls-remote": (128, STDERR_PRIVATE)})

    with pytest.raises(RemoteNotReadable):
        remote.fetch_pull_request(42)


# --- opening a source -----------------------------------------------------------------

def test_open_source_picks_a_checkout_or_a_remote_from_what_it_is_given(tmp_path: Path) -> None:
    checkout = tmp_path / "somewhere"
    checkout.mkdir()

    assert isinstance(open_source(str(checkout)), LocalCheckout)
    remote = open_source(BUILDIMAGE, cache_root=tmp_path / "cache")
    assert isinstance(remote, RemoteRepo)
    assert remote.url == BUILDIMAGE_URL


def test_a_remote_describes_where_it_reads_from(cache_root: Path) -> None:
    remote = RemoteRepo(BUILDIMAGE, cache_root=cache_root)
    assert BUILDIMAGE_URL in remote.describe
    assert str(remote.cache_dir) in remote.describe
