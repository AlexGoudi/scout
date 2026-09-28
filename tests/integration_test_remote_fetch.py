"""Network-dependent proof that the remote fetch path is real, and that it is cheap.

**Not part of the offline suite.** The filename is outside the `unit_test_*.py` glob the
default run uses, and every test here additionally skips unless `$SCOUT_NETWORK_TESTS=1`,
so `python3 -m pytest tests/unit_test_*.py -q` stays hermetic (NFR-10).

    SCOUT_NETWORK_TESTS=1 python3 -m pytest tests/integration_test_*.py -q -s

Everything runs anonymously against a public GitHub remote: no API key, no `gh`, no
credentials of any kind. `$SCOUT_NETWORK_REMOTE` overrides the repository fetched.

What these prove that the offline tests cannot: that the shallow blob-filtered fetch
works against the real remote, and that a recursive tree listing afterwards downloads
nothing while a file read does. The assertions on cost are deliberately loose — they are
there to catch a regression that turns a listing into a download, not to pin a number to
one machine's network.
"""

import time
from pathlib import Path

import pytest

from scout_impl.ingest import resolve
from scout_impl.models import ChangeSetSpec
from scout_impl.remote import PullRequestNotFound, RemoteRepo, RemoteUnreachable
from scout_impl.repos import get_adapter

# Merged, so its refs are immutable and this test does not drift with upstream.
MERGED_PULL_REQUEST = 21843
MISSING_PULL_REQUEST = 99999999


def _remote(url: str, cache: Path) -> RemoteRepo:
    return RemoteRepo(url, cache_root=cache)


def _local_objects(remote: RemoteRepo) -> int:
    """How many objects the cache actually holds.

    A better measure of "did that download anything" than bytes on disk, which git
    changes on its own whenever it repacks. Enumerating what is present never asks the
    promisor remote for what is not.
    """
    return len(remote.git("cat-file", "--batch-all-objects", "--batch-check=%(objectname)").split())


def test_a_pull_request_fetches_shallow_and_small(network_remote: str, network_cache: Path) -> None:
    remote = _remote(network_remote, network_cache)

    started = time.monotonic()
    fetched = remote.fetch_pull_request(MERGED_PULL_REQUEST)
    elapsed = time.monotonic() - started
    megabytes = remote.cache_size_bytes() / 1e6

    print(f"\nfetch   pr#{MERGED_PULL_REQUEST}: {elapsed:.2f}s, depth {fetched.depth}/{fetched.base_depth}, "
          f"{fetched.fetches} round(s), {megabytes:.2f} MB on disk")

    assert len(fetched.head_sha) == 40
    assert len(fetched.base_sha) == 40
    assert fetched.base_sha != fetched.head_sha
    # Shallow means shallow: a full sonic-buildimage clone is several gigabytes.
    assert megabytes < 100, "the fetch stopped being shallow or stopped filtering blobs"


def test_listing_the_tree_downloads_nothing_and_reading_a_file_does(
    network_remote: str, network_cache: Path
) -> None:
    """The property the whole design rests on, measured rather than asserted."""
    remote = _remote(network_remote, network_cache)
    head = remote.fetch_pull_request(MERGED_PULL_REQUEST).head_sha
    after_fetch = _local_objects(remote)

    started = time.monotonic()
    paths = remote.list_paths(head)
    listing_seconds = time.monotonic() - started
    after_listing = _local_objects(remote)

    print(f"ls-tree {len(paths)} paths: {listing_seconds:.3f}s, "
          f"{after_listing - after_fetch} object(s) downloaded")

    assert len(paths) > 1000
    assert after_listing == after_fetch, "a tree listing pulled objects; --filter=blob:none is not holding"
    assert remote.blob_reads == 0

    started = time.monotonic()
    content = remote.read_file(head, "rules/config")
    read_seconds = time.monotonic() - started
    after_read = _local_objects(remote)

    print(f"read    1 file: {read_seconds:.3f}s, {after_read - after_listing} object(s) downloaded")

    assert content.strip(), "read_file returned nothing"
    assert remote.blob_reads == 1
    assert after_read > after_listing, "reading a file fetched no blob; was it not really deferred?"


def test_the_coverage_gap_query_needs_no_file_content(network_remote: str, network_cache: Path) -> None:
    """Which HWSKU directories carry a port_config.ini but no pg_profile_lookup.ini.

    The question Scout has to answer over a repository it never cloned, answered from
    tree metadata alone.
    """
    remote = _remote(network_remote, network_cache)
    head = remote.fetch_pull_request(MERGED_PULL_REQUEST).head_sha
    before = _local_objects(remote)

    started = time.monotonic()
    with_ports = set(remote.directories_containing(head, "port_config.ini"))
    with_profiles = set(remote.directories_containing(head, "pg_profile_lookup.ini"))
    gap = with_ports - with_profiles
    elapsed = time.monotonic() - started

    print(f"gap     {len(with_ports)} HWSKU dirs with port_config.ini, {len(with_profiles)} with "
          f"pg_profile_lookup.ini, {len(gap)} without: {elapsed:.3f}s, "
          f"{_local_objects(remote) - before} object(s) downloaded")

    assert with_ports, "found no HWSKU directories; the tree listing is wrong"
    assert remote.blob_reads == 0
    assert _local_objects(remote) == before


def test_the_repository_identifies_itself_from_the_fetched_tree(
    network_remote: str, network_cache: Path
) -> None:
    remote = _remote(network_remote, network_cache)
    head = remote.fetch_pull_request(MERGED_PULL_REQUEST).head_sha

    assert remote.detect_adapter(head) is get_adapter("sonic-buildimage")
    assert remote.blob_reads == 0


def test_ingest_resolves_a_pull_request_with_no_clone(network_remote: str, network_cache: Path) -> None:
    """End to end: a pull request nobody cloned becomes a classified `ChangeSet`."""
    remote = _remote(network_remote, network_cache)
    fetched = remote.fetch_pull_request(MERGED_PULL_REQUEST)

    started = time.monotonic()
    change_set = resolve(
        ChangeSetSpec(base_ref=fetched.base_sha, head_ref=fetched.head_sha),
        remote,
    )
    elapsed = time.monotonic() - started

    print(f"ingest  {change_set.commit_count} commit(s), {len(change_set.changed_paths)} path(s): "
          f"{elapsed:.2f}s, repo identified as {change_set.repo}")

    assert change_set.repo == "sonic-buildimage"
    assert change_set.commit_count >= 1
    for commit in change_set.commits:
        for file_diff in commit.files:
            assert file_diff.path_class.repo == "sonic-buildimage"
            assert file_diff.path_class.rank >= 1


def test_the_cache_is_reused_rather_than_refetched(network_remote: str, tmp_path: Path) -> None:
    """A second run over the same pull request must reuse the cache, not rebuild it."""
    cache = tmp_path / "cold-cache"
    first = _remote(network_remote, cache)
    started = time.monotonic()
    cold = first.fetch_pull_request(MERGED_PULL_REQUEST)
    cold_seconds = time.monotonic() - started
    cold_objects = _local_objects(first)

    second = _remote(network_remote, cache)
    started = time.monotonic()
    warm = second.fetch_pull_request(MERGED_PULL_REQUEST)
    warm_seconds = time.monotonic() - started

    print(f"cache   cold {cold_seconds:.2f}s in {cold.fetches} round(s), warm {warm_seconds:.2f}s in "
          f"{warm.fetches} round(s), {_local_objects(second) - cold_objects} object(s) added")

    assert second.cache_dir == first.cache_dir
    assert (warm.base_sha, warm.head_sha) == (cold.base_sha, cold.head_sha)
    assert warm.fetches == 0, "a cached remote went back to the network"
    assert _local_objects(second) == cold_objects


def test_a_pull_request_that_does_not_exist_says_so(network_remote: str, network_cache: Path) -> None:
    remote = _remote(network_remote, network_cache)

    with pytest.raises(PullRequestNotFound) as error:
        remote.fetch_pull_request(MISSING_PULL_REQUEST)
    assert str(MISSING_PULL_REQUEST) in str(error.value)


def test_an_unreachable_host_says_so(network_remote: str, network_cache: Path) -> None:
    remote = RemoteRepo("https://scout-no-such-host.invalid/x/y.git", cache_root=network_cache)

    with pytest.raises(RemoteUnreachable):
        remote.ls_remote("HEAD")
