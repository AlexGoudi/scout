import json
from pathlib import Path

from scout_impl.gitcmd import GitRepo
from scout_impl.incidents import (
    LINK_LINKED,
    LINK_UNLINKED,
    LINK_UNRESOLVED,
    mine_incidents,
    read_corpus,
    summarize,
    write_corpus,
)

from conftest import TempGitRepo

MISSING_SHA = "0123456789abcdef0123456789abcdef01234567"


def _history(temp_repo: TempGitRepo) -> str:
    """A cause commit, its revert 59 days later, and two reverts that cannot be linked."""
    temp_repo.write("ansible/library/generate_golden_config_db.py", "ADMIN_STATUS = 'down'\n")
    temp_repo.commit("Add golden config generator (#100)", date="2026-01-01T00:00:00+00:00")

    temp_repo.write("ansible/library/generate_golden_config_db.py", "ADMIN_STATUS = 'up'\n")
    temp_repo.write("ansible/vars/topo_mx.yml", "topology: {}\n")
    cause = temp_repo.commit("Default undeclared ports to up (#23052)", date="2026-01-01T12:00:00+00:00")

    temp_repo.write("ansible/library/generate_golden_config_db.py", "ADMIN_STATUS = 'down'\n")
    temp_repo.commit(
        f'Revert "Default undeclared ports to up (#23052)" (#23400)\n\nThis reverts commit {cause}.\n',
        date="2026-03-01T12:00:00+00:00",
    )

    temp_repo.write("tests/bgp/test_bgp_fact.py", "def test_bgp_fact():\n    pass\n")
    temp_repo.commit("Revert the bgp behaviour by hand (#23401)", date="2026-03-02T00:00:00+00:00")

    temp_repo.write("tests/bgp/test_bgp_fact.py", "def test_bgp_fact():\n    assert True\n")
    temp_repo.commit(
        f'Revert "A change that lives on another branch"\n\nThis reverts commit {MISSING_SHA}.\n',
        date="2026-03-03T00:00:00+00:00",
    )
    return cause


def test_miner_links_reverts_to_their_cause(temp_repo: TempGitRepo) -> None:
    cause = _history(temp_repo)

    incidents = mine_incidents(temp_repo.as_repo())

    assert [incident.link_status for incident in incidents] == [
        LINK_UNRESOLVED,
        LINK_UNLINKED,
        LINK_LINKED,
    ]
    assert [incident.incident_id for incident in incidents] == [
        "incident-0001",
        "incident-0002",
        "incident-0003",
    ]

    linked = incidents[-1]
    assert linked.reverted_sha == cause
    assert linked.reverted_subject == "Default undeclared ports to up (#23052)"
    assert linked.revert_subject.startswith('Revert "Default undeclared ports to up')
    assert linked.reverted_committed_date.startswith("2026-01-01")
    assert linked.revert_committed_date.startswith("2026-03-01")
    assert linked.lead_time_days == 59
    assert linked.pr_number == 23400
    assert sorted(linked.reverted_paths) == [
        "ansible/library/generate_golden_config_db.py",
        "ansible/vars/topo_mx.yml",
    ]
    assert linked.revert_paths == ["ansible/library/generate_golden_config_db.py"]


def test_detector_category_is_left_for_the_triage_session(temp_repo: TempGitRepo) -> None:
    _history(temp_repo)

    incidents = mine_incidents(temp_repo.as_repo())

    assert all(incident.detector_category is None for incident in incidents)
    assert summarize(incidents)["categorized"] == 0


def test_unlinkable_reverts_are_kept_without_derived_fields(temp_repo: TempGitRepo) -> None:
    _history(temp_repo)

    incidents = {incident.link_status: incident for incident in mine_incidents(temp_repo.as_repo())}

    unlinked = incidents[LINK_UNLINKED]
    assert unlinked.reverted_sha is None
    assert unlinked.lead_time_days is None
    assert unlinked.revert_paths == ["tests/bgp/test_bgp_fact.py"]

    unresolved = incidents[LINK_UNRESOLVED]
    assert unresolved.reverted_sha == MISSING_SHA
    assert unresolved.reverted_subject is None
    assert unresolved.lead_time_days is None


def test_nested_reverts_are_flagged(temp_repo: TempGitRepo) -> None:
    cause = _history(temp_repo)
    temp_repo.write("ansible/library/generate_golden_config_db.py", "ADMIN_STATUS = 'up'\n")
    temp_repo.commit(
        f'Revert "Revert "Default undeclared ports to up (#23052)" (#23400)"\n\n'
        f"This reverts commit {cause}.\n",
        date="2026-03-04T00:00:00+00:00",
    )

    incidents = mine_incidents(temp_repo.as_repo())

    assert incidents[0].is_nested_revert is True
    assert sum(1 for incident in incidents if incident.is_nested_revert) == 1


def test_multiple_revert_trailers_keep_the_extras(temp_repo: TempGitRepo) -> None:
    temp_repo.write("tests/bgp/a.py", "A = 1\n")
    first = temp_repo.commit("First change (#1)")
    temp_repo.write("tests/bgp/b.py", "B = 1\n")
    second = temp_repo.commit("Second change (#2)")
    temp_repo.write("tests/bgp/a.py", "A = 0\n")
    temp_repo.commit(
        f"Revert two changes (#3)\n\nThis reverts commit {first}.\nThis reverts commit {second}.\n"
    )

    incident = mine_incidents(temp_repo.as_repo())[0]

    assert incident.reverted_sha == first
    assert incident.additional_reverted_shas == [second]


def test_grep_and_limit_narrow_the_scan(temp_repo: TempGitRepo) -> None:
    _history(temp_repo)

    assert len(mine_incidents(temp_repo.as_repo(), limit=2)) == 2
    assert mine_incidents(temp_repo.as_repo(), grep="^Nothing matches this") == []


def test_corpus_round_trips_through_jsonl(temp_repo: TempGitRepo, tmp_path: Path) -> None:
    _history(temp_repo)
    incidents = mine_incidents(temp_repo.as_repo())
    corpus_path = tmp_path / "corpus" / "scout-corpus.jsonl"

    write_corpus(incidents, corpus_path)
    restored = read_corpus(corpus_path)

    assert [incident.to_dict() for incident in restored] == [incident.to_dict() for incident in incidents]

    lines = corpus_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(incidents)
    assert json.loads(lines[0])["detector_category"] is None


def test_summary_counts_match_the_records(temp_repo: TempGitRepo) -> None:
    _history(temp_repo)

    summary = summarize(mine_incidents(temp_repo.as_repo()))

    assert summary["reverts"] == 3
    assert summary["linked"] == 1
    assert summary["unlinked"] == 1
    assert summary["unresolved"] == 1
    assert summary["with_pr_number"] == 2
    assert summary["lead_time_days"] == {
        "count": 1,
        "negative": 0,
        "min": 59,
        "median": 59,
        "max": 59,
    }


def test_mining_the_target_repository(target_repo: Path) -> None:
    incidents = mine_incidents(GitRepo(target_repo))
    summary = summarize(incidents)

    assert summary["reverts"] > 100
    assert summary["linked"] + summary["unresolved"] > 100
    assert summary["linked"] + summary["unlinked"] + summary["unresolved"] == summary["reverts"]
    for incident in incidents:
        assert len(incident.revert_sha) == 40
        assert incident.detector_category is None
        if incident.link_status == LINK_LINKED:
            assert incident.reverted_sha and incident.reverted_sha != incident.revert_sha
            assert incident.lead_time_days is not None
