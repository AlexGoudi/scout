import json
from pathlib import Path

import pytest

from scout_impl.gitcmd import GitError
from scout_impl.ingest import resolve
from scout_impl.models import (
    CHANGE_ADDED,
    CHANGE_RENAMED,
    ChangeSet,
    ChangeSetSpec,
    MODE_SYNC,
)
from scout_impl.repos import resolve_adapter
from scout_impl.repos.sonic_mgmt import ANSIBLE_CODE, ANSIBLE_DATA

from conftest import TempGitRepo

# The throwaway repositories these tests build are shaped like sonic-mgmt but carry none
# of its marker files, so they name the adapter instead of letting Scout identify one.
SONIC_MGMT = resolve_adapter("sonic-mgmt")

TOPO_BODY = "topology:\n  host_interfaces: [0, 1]\n  VMs: {}\n"
TOPO_BODY_WITH_DUT_TYPE = TOPO_BODY + "  dut_type: MgmtToRRouter\n"


def _seed(repo: TempGitRepo) -> str:
    repo.write("ansible/vars/topo_mx.yml", TOPO_BODY)
    repo.write("tests/bgp/test_bgp_fact.py", "def test_bgp_fact():\n    pass\n")
    return repo.commit("Initial import")


def test_range_ingest_orders_commits_and_classifies_paths(temp_repo: TempGitRepo) -> None:
    base = _seed(temp_repo)
    temp_repo.write("ansible/library/generate_golden_config_db.py", "PORT = {'admin_status': 'down'}\n")
    first = temp_repo.commit("Add golden config generator")
    temp_repo.write("ansible/library/generate_golden_config_db.py", "PORT = {'admin_status': 'up'}\n")
    second = temp_repo.commit("Default ports to admin_status up")

    change_set = resolve(ChangeSetSpec.from_range(f"{base}..HEAD"), temp_repo.root, SONIC_MGMT)

    assert change_set.base_sha == base
    assert change_set.head_sha == second
    assert [commit.sha for commit in change_set.commits] == [first, second]
    assert change_set.commits[0].subject == "Add golden config generator"
    assert change_set.commits[0].files[0].change_type == CHANGE_ADDED
    assert change_set.commits[0].highest_path_class == ANSIBLE_CODE
    assert change_set.changed_paths == ["ansible/library/generate_golden_config_db.py"]


def test_ingest_captures_line_numbers_for_citations(temp_repo: TempGitRepo) -> None:
    base = _seed(temp_repo)
    temp_repo.write("ansible/vars/topo_mx.yml", TOPO_BODY_WITH_DUT_TYPE)
    temp_repo.commit("Declare dut_type on topo_mx")

    change_set = resolve(ChangeSetSpec.from_range(f"{base}..HEAD"), temp_repo.root, SONIC_MGMT)

    file_diff = change_set.commits[0].files[0]
    assert file_diff.path == "ansible/vars/topo_mx.yml"
    assert file_diff.path_class == ANSIBLE_DATA
    added = file_diff.added_lines
    assert [line.content for line in added] == ["  dut_type: MgmtToRRouter"]
    assert [line.new_lineno for line in added] == [4]


def test_ingest_detects_renames(temp_repo: TempGitRepo) -> None:
    base = _seed(temp_repo)
    temp_repo.git("mv", "ansible/vars/topo_mx.yml", "ansible/vars/topo_mx_renamed.yml")
    temp_repo.commit("Rename the mx topology")

    change_set = resolve(ChangeSetSpec.from_range(f"{base}..HEAD"), temp_repo.root, SONIC_MGMT)

    file_diff = change_set.commits[0].files[0]
    assert file_diff.change_type == CHANGE_RENAMED
    assert file_diff.path == "ansible/vars/topo_mx_renamed.yml"
    assert file_diff.old_path == "ansible/vars/topo_mx.yml"


def test_merge_commits_are_excluded_by_default(temp_repo: TempGitRepo) -> None:
    base = _seed(temp_repo)
    temp_repo.git("checkout", "-q", "-b", "side")
    temp_repo.write("tests/common/helpers/side.py", "SIDE = 1\n")
    side = temp_repo.commit("Add a shared helper")
    temp_repo.git("checkout", "-q", "main")
    temp_repo.write("tests/bgp/test_bgp_fact.py", "def test_bgp_fact():\n    assert True\n")
    main = temp_repo.commit("Strengthen the bgp assertion")
    temp_repo.git("merge", "-q", "--no-ff", "-m", "Merge side into main", "side")

    change_set = resolve(ChangeSetSpec.from_range(f"{base}..HEAD"), temp_repo.root, SONIC_MGMT)
    assert sorted(commit.sha for commit in change_set.commits) == sorted([side, main])

    with_merges = resolve(
        ChangeSetSpec.from_range(f"{base}..HEAD", include_merges=True), temp_repo.root, SONIC_MGMT
    )
    assert len(with_merges.commits) == 3


def test_sync_mode_flags_local_patches_overlapping_the_delta(temp_repo: TempGitRepo) -> None:
    _seed(temp_repo)
    temp_repo.write("ansible/library/topo_facts.py", "VERSION = 1\n")
    temp_repo.commit("Upstream baseline")

    temp_repo.git("checkout", "-q", "-b", "fork")
    temp_repo.write("ansible/library/topo_facts.py", "VERSION = 1\nLOCAL_FIX = True\n")
    overlapping = temp_repo.commit("Local fix to topo_facts")
    temp_repo.write("tests/bgp/test_local_only.py", "def test_local():\n    pass\n")
    unrelated = temp_repo.commit("Local-only test")

    temp_repo.git("checkout", "-q", "main")
    temp_repo.write("ansible/library/topo_facts.py", "VERSION = 2\n")
    temp_repo.commit("Upstream rewrites topo_facts")

    spec = ChangeSetSpec(base_ref="fork", head_ref="main", mode=MODE_SYNC, merge_base=True)
    change_set = resolve(spec, temp_repo.root, SONIC_MGMT)

    assert change_set.mode == MODE_SYNC
    assert [commit.subject for commit in change_set.commits] == ["Upstream rewrites topo_facts"]

    by_sha = {patch.sha: patch for patch in change_set.local_patches}
    assert set(by_sha) == {overlapping, unrelated}
    assert by_sha[overlapping].overlapping_paths == ["ansible/library/topo_facts.py"]
    assert by_sha[unrelated].overlapping_paths == []


def test_change_set_round_trips_through_json(temp_repo: TempGitRepo) -> None:
    base = _seed(temp_repo)
    temp_repo.write("ansible/library/generate_golden_config_db.py", "PORT = {}\n")
    temp_repo.commit("Add golden config generator")

    original = resolve(ChangeSetSpec.from_range(f"{base}..HEAD"), temp_repo.root, SONIC_MGMT)
    payload = json.loads(json.dumps(original.to_dict()))
    restored = ChangeSet.from_dict(payload)

    assert restored.to_dict() == original.to_dict()
    assert restored.commits[0].files[0].path_class == ANSIBLE_CODE
    assert restored.spec.to_dict() == original.spec.to_dict()
    # The adapter identity has to survive the round trip too, or a cached change set
    # cannot say whose rules produced its path classes.
    assert restored.repo == SONIC_MGMT.name
    assert restored.adapter is SONIC_MGMT


def test_root_commit_is_diffed_against_the_empty_tree(temp_repo: TempGitRepo) -> None:
    _seed(temp_repo)
    temp_repo.git("checkout", "-q", "--orphan", "unrelated")
    orphan = temp_repo.commit("Unrelated history")
    temp_repo.git("checkout", "-q", "main")

    change_set = resolve(ChangeSetSpec.from_range(f"{orphan}..main"), temp_repo.root, SONIC_MGMT)

    assert change_set.commits[0].parents == []
    assert [file_diff.path for file_diff in change_set.commits[0].files] == [
        "ansible/vars/topo_mx.yml",
        "tests/bgp/test_bgp_fact.py",
    ]
    assert all(file_diff.change_type == CHANGE_ADDED for file_diff in change_set.commits[0].files)


def test_unknown_revision_raises_git_error(temp_repo: TempGitRepo) -> None:
    _seed(temp_repo)

    with pytest.raises(GitError):
        resolve(ChangeSetSpec.from_range("nope..HEAD"), temp_repo.root, SONIC_MGMT)


def test_range_expression_requires_a_base() -> None:
    with pytest.raises(ValueError):
        ChangeSetSpec.from_range("HEAD")
    with pytest.raises(ValueError):
        ChangeSetSpec.from_range("..HEAD")


def test_three_dot_range_uses_the_merge_base(temp_repo: TempGitRepo) -> None:
    fork_point = _seed(temp_repo)
    temp_repo.git("checkout", "-q", "-b", "feature")
    temp_repo.write("tests/bgp/test_new.py", "def test_new():\n    pass\n")
    feature = temp_repo.commit("Add a feature test")
    temp_repo.git("checkout", "-q", "main")
    temp_repo.write("ansible/vars/topo_t0.yml", TOPO_BODY)
    temp_repo.commit("Unrelated main commit")

    change_set = resolve(ChangeSetSpec.from_range("main...feature"), temp_repo.root, SONIC_MGMT)

    assert change_set.base_sha == fork_point
    assert [commit.sha for commit in change_set.commits] == [feature]


def test_ingest_of_the_target_repository(target_repo: Path) -> None:
    change_set = resolve(ChangeSetSpec.from_range("HEAD~3..HEAD"), target_repo)

    assert change_set.commit_count <= 3
    assert change_set.head_sha != change_set.base_sha
    for commit in change_set.commits:
        assert len(commit.sha) == 40
        assert commit.authored_date
        for file_diff in commit.files:
            assert file_diff.path
            assert file_diff.path_class.rank >= 1
