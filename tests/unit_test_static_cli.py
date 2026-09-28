"""The `brief` command, driven offline from a pinned fixture.

The CLI is the surface a developer actually touches (S1, FR-12), so the assertions here
are about what it writes and what it does when asked for something it cannot do, not
about the analysis — that is the conformance suite's job.
"""

import json
from pathlib import Path

import pytest

from scout_impl.cli import run

TREES = Path(__file__).resolve().parent / "fixtures" / "trees"
UPSTREAM = TREES / "sonic-buildimage-master-62cfe50.json"


def _run(monkeypatch, *args):
    monkeypatch.setattr("sys.argv", ["run_scout.py", *args])
    return run()


def test_the_brief_command_writes_a_schema_valid_brief_from_a_fixture(monkeypatch, tmp_path):
    output = tmp_path / "scout-brief.json"
    assert _run(monkeypatch, "brief", "--fixture", str(UPSTREAM), "--output", str(output)) == 0

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "1.0"
    # 287, not 284: rule C6 counts the three aliased platform directories. Asserted here
    # alongside the declaration count so the two quantities stay visibly apart.
    assert payload["coverage"]["platforms_in_tree"] == 287
    assert payload["coverage"]["declarations_in_tree"] == 287
    assert payload["coverage"]["aliased_platforms"] == 3
    assert payload["coverage"]["job_groups"] == 9


def test_the_brief_command_can_print_instead_of_writing(monkeypatch, capsys, tmp_path):
    assert _run(monkeypatch, "brief", "--fixture", str(UPSTREAM), "--stdout") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["brief"]["repo"] == "sonic-net/sonic-buildimage"
    assert not (tmp_path / "scout-brief.json").exists()


def test_the_hotspot_cap_is_honoured(monkeypatch, tmp_path):
    """NFR-4 caps what gets reported; the flag is how a developer widens it locally."""
    output = tmp_path / "brief.json"
    assert _run(monkeypatch, "brief", "--fixture", str(UPSTREAM), "--hotspots", "3",
                "--output", str(output)) == 0
    assert len(json.loads(output.read_text(encoding="utf-8"))["hotspots"]) <= 3


def test_asking_for_a_pull_request_without_a_remote_is_refused(monkeypatch, tmp_path):
    assert _run(monkeypatch, "--repo-root", str(tmp_path), "brief", "--pr", "1") == 1


def test_asking_for_two_change_sets_at_once_is_refused(monkeypatch):
    assert _run(monkeypatch, "brief", "--fixture", str(UPSTREAM),
                "--rev", "HEAD", "--range", "a..b") == 1


def test_a_missing_fixture_names_the_file_rather_than_tracing(monkeypatch, tmp_path):
    assert _run(monkeypatch, "brief", "--fixture", str(tmp_path / "absent.json")) == 1


def test_the_command_is_listed_alongside_the_existing_ones(monkeypatch, capsys):
    with pytest.raises(SystemExit):
        _run(monkeypatch, "--help")
    help_text = capsys.readouterr().out
    for command in ("ingest", "tree", "brief", "mine-incidents"):
        assert command in help_text
