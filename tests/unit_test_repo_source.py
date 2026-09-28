"""The one interface ingest reads through, exercised against a local working copy.

`LocalCheckout` and `RemoteRepo` share every method tested here except `git` itself, so
proving the tree and file operations against a throwaway repository proves them for both
without a network. What cannot be proved offline — that a listing really does transfer
nothing against a blob-filtered remote — is in `tests/integration_test_remote_fetch.py`.
"""

from pathlib import Path

import pytest

from scout_impl.gitcmd import GitError
from scout_impl.repos import RepoAdapterError, get_adapter
from scout_impl.source import KIND_BLOB, LocalCheckout, RepoSource, TreeEntry, as_source

from conftest import TempGitRepo

HWSKUS_WITH_PORTS = ("Nokia-IXR7250E-36x400G", "Arista-7050-QX32", "Celestica-DX010")
HWSKUS_WITH_PROFILES = ("Nokia-IXR7250E-36x400G",)


def _buildimage_shaped(repo: TempGitRepo) -> str:
    """A tree with buildimage's markers and the HWSKU shape the coverage gap asks about."""
    repo.write("slave.mk", "SONIC_BUILD_JOBS ?= 1\n")
    repo.write("Makefile.work", "include slave.mk\n")
    repo.write("rules/config", "CONFIGURED_PLATFORM ?= generic\n")
    repo.write("platform/broadcom/sai.mk", "LIBSAIBCM_VERSION = 1\n")
    for hwsku in HWSKUS_WITH_PORTS:
        repo.write(f"device/vendor/x86_64-platform-r0/{hwsku}/port_config.ini", "# name lanes alias index\n")
    for hwsku in HWSKUS_WITH_PROFILES:
        repo.write(f"device/vendor/x86_64-platform-r0/{hwsku}/pg_profile_lookup.ini", "# speed cable size\n")
    return repo.commit("Buildimage-shaped tree")


def test_a_path_is_accepted_wherever_a_source_is(temp_repo: TempGitRepo) -> None:
    _buildimage_shaped(temp_repo)
    source = as_source(temp_repo.root)

    assert isinstance(source, LocalCheckout)
    assert source.root == temp_repo.root
    assert as_source(source) is source
    with pytest.raises(TypeError):
        as_source(object())


def test_list_tree_reports_metadata_and_never_content(temp_repo: TempGitRepo) -> None:
    head = _buildimage_shaped(temp_repo)
    source = LocalCheckout(temp_repo.root)

    entries = source.list_tree(head)
    by_path = {entry.path: entry for entry in entries}

    assert "slave.mk" in by_path
    assert isinstance(by_path["slave.mk"], TreeEntry)
    assert by_path["slave.mk"].kind == KIND_BLOB
    assert by_path["slave.mk"].is_file
    assert not by_path["slave.mk"].is_submodule
    assert len(by_path["slave.mk"].sha) == 40
    # A listing is not a read: nothing here has cost a blob.
    assert source.blob_reads == 0


def test_a_prefix_narrows_the_listing(temp_repo: TempGitRepo) -> None:
    head = _buildimage_shaped(temp_repo)
    source = LocalCheckout(temp_repo.root)

    assert all(path.startswith("device/") for path in source.list_paths(head, "device"))
    assert len(source.list_paths(head, "device")) < len(source.list_paths(head))


def test_the_coverage_gap_shape_is_answerable_from_tree_metadata(temp_repo: TempGitRepo) -> None:
    """The motivating query: which directories carry one marker file but not another."""
    head = _buildimage_shaped(temp_repo)
    source = LocalCheckout(temp_repo.root)

    with_ports = set(source.directories_containing(head, "port_config.ini"))
    with_profiles = set(source.directories_containing(head, "pg_profile_lookup.ini"))

    assert len(with_ports) == len(HWSKUS_WITH_PORTS)
    assert len(with_profiles) == len(HWSKUS_WITH_PROFILES)
    gap = {Path(directory).name for directory in with_ports - with_profiles}
    assert gap == {"Arista-7050-QX32", "Celestica-DX010"}
    assert source.blob_reads == 0


def test_paths_matching_globs_across_directory_separators(temp_repo: TempGitRepo) -> None:
    head = _buildimage_shaped(temp_repo)
    source = LocalCheckout(temp_repo.root)

    assert source.paths_matching(head, "*.mk") == ["platform/broadcom/sai.mk", "slave.mk"]
    assert len(source.paths_matching(head, "device/*/port_config.ini")) == len(HWSKUS_WITH_PORTS)
    assert source.paths_matching(head, "*/no-such-file") == []
    assert source.blob_reads == 0


def test_reading_a_file_is_the_operation_that_counts_as_expensive(temp_repo: TempGitRepo) -> None:
    head = _buildimage_shaped(temp_repo)
    source = LocalCheckout(temp_repo.root)

    assert source.read_file(head, "rules/config") == "CONFIGURED_PLATFORM ?= generic\n"
    assert source.blob_reads == 1
    source.read_file(head, "slave.mk")
    assert source.blob_reads == 2


def test_revisions_resolve_and_missing_ones_say_where_they_were_looked_for(temp_repo: TempGitRepo) -> None:
    head = _buildimage_shaped(temp_repo)
    source = LocalCheckout(temp_repo.root)

    assert source.rev_parse("HEAD") == head
    with pytest.raises(GitError) as error:
        source.rev_parse("no-such-ref")
    assert str(temp_repo.root) in str(error.value)


def test_a_source_identifies_its_repository_from_the_tree(temp_repo: TempGitRepo) -> None:
    head = _buildimage_shaped(temp_repo)
    source = LocalCheckout(temp_repo.root)

    assert source.detect_adapter(head) is get_adapter("sonic-buildimage")
    assert source.blob_reads == 0


def test_an_unrecognizable_tree_says_to_name_the_repository(temp_repo: TempGitRepo) -> None:
    temp_repo.write("src/main.rs", "fn main() {}\n")
    head = temp_repo.commit("Not a SONiC repository")

    with pytest.raises(RepoAdapterError) as error:
        LocalCheckout(temp_repo.root).detect_adapter(head)
    assert "name one of" in str(error.value)


def test_the_interface_is_abstract_so_a_source_cannot_forget_to_implement_it() -> None:
    with pytest.raises(TypeError):
        RepoSource()
