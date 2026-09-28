"""The two recorded demo cases replay with no network and no model, and reproduce the live runs.

Each case under `tests/fixtures/demo/` was reviewed live against the local model by
`tests/integration_test_review_ollama.py` and pinned: the recorded responses, the change
set, a tree fixture per side, and the live run's own artifacts under `live/`. These tests
replay each one offline and hold the replay to the live run byte for byte, apart from the
timings a rerun cannot reproduce, so a change to any stage that would alter what the demo
shows fails here rather than on the day the recording is played back.

    python3 run_scout.py review --fixture tests/fixtures/demo/pmon-24811/review.json --provider replay
"""

import json
import socket
from pathlib import Path

import pytest

from scout_impl.cli import run
from scout_impl.core.review import COMMENT_FILE, REPORT_FILE, review_fixture
from scout_impl.core.review_fixture import MANIFEST, REPLAY_DIR, ReviewFixture
from scout_impl.provider import ReplayProvider
from scout_impl.report.builder import Report
from scout_impl.static.brief import Brief

DEMO = Path(__file__).resolve().parent / "fixtures" / "demo"
PMON = DEMO / "pmon-24811"
PRESTERA = DEMO / "prestera-20860"
CASES = (PMON, PRESTERA)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a demo replay must not open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def _replay(folder, tmp_path, provider=True):
    return review_fixture(folder / MANIFEST,
                          provider=ReplayProvider.from_fixtures(folder / REPLAY_DIR) if provider else None,
                          output_dir=tmp_path / folder.name)


def _live(folder, name):
    return (folder / "live" / name).read_text(encoding="utf-8")


def _finding(result, kind):
    question = next(item for item in result.brief.payload["questions"] if item["kind"] == kind)
    return next(finding for finding in result.report.findings if finding["question"] == question["id"])


@pytest.mark.parametrize("folder", CASES, ids=[folder.name for folder in CASES])
def test_the_replay_reproduces_the_live_run(folder, tmp_path):
    result = _replay(folder, tmp_path)
    assert result.brief.canonical_json() == Brief(json.loads(_live(folder, "scout-brief.json"))).canonical_json()
    assert result.report.canonical_json() == Report(json.loads(_live(folder, "scout-report.json"))).canonical_json()
    assert result.comment == _live(folder, "scout-comment.md")
    calls = [call for question in result.agent.questions for call in question.calls]
    assert calls and all(call.cached for call in calls)
    assert result.report.payload["run"]["model"]["replayed"] is True


@pytest.mark.parametrize("folder", CASES, ids=[folder.name for folder in CASES])
def test_the_fixture_names_a_real_change_and_both_its_sides(folder):
    fixture = ReviewFixture.load(folder / MANIFEST)
    assert fixture.repo == "sonic-net/sonic-buildimage" and fixture.adapter == "sonic-buildimage"
    assert fixture.change_set.commit_count == 1
    assert fixture.head.rev == fixture.change_set.head_sha
    assert fixture.base is None or fixture.base.rev == fixture.change_set.base_sha


@pytest.mark.parametrize("folder", CASES, ids=[folder.name for folder in CASES])
def test_with_no_model_the_demo_degrades_to_the_same_facts(folder, tmp_path):
    live = _replay(folder, tmp_path)
    degraded = _replay(folder, tmp_path / "none", provider=False)
    assert degraded.report.status == "degraded" and degraded.agent.model_calls == 0
    assert [finding["deterministic"] for finding in degraded.report.findings] == [
        finding["deterministic"] for finding in live.report.findings]
    assert degraded.brief.canonical_json() == live.brief.canonical_json()


@pytest.mark.parametrize("folder", CASES, ids=[folder.name for folder in CASES])
def test_the_cli_replays_a_demo_offline(folder, monkeypatch, tmp_path):
    monkeypatch.setattr("sys.argv", ["run_scout.py", "review", "--fixture", str(folder / MANIFEST),
                                     "--provider", "replay", "--output-dir", str(tmp_path)])
    assert run() == 0
    written = Report(json.loads((tmp_path / REPORT_FILE).read_text(encoding="utf-8")))
    assert written.canonical_json() == Report(json.loads(_live(folder, "scout-report.json"))).canonical_json()
    assert (tmp_path / COMMENT_FILE).read_text(encoding="utf-8") == _live(folder, "scout-comment.md")


# --- what each demo shows ---------------------------------------------------------------------


def test_pmon_reverse_reach_turns_one_shared_file_into_38_platforms(tmp_path):
    """#24811 edits one file in a shared directory, which is no platform at all; the platforms
    that link it are what it reaches, and 14 of them no PR-CI job group builds."""
    result = _replay(PMON, tmp_path)
    coverage = result.brief.coverage
    assert (len(coverage["affected"]), len(coverage["covered"]), len(coverage["uncovered"]),
            len(coverage["ambiguous"])) == (38, 24, 14, 0)
    assert coverage["job_group_names"] == ["broadcom", "marvell-prestera-arm64", "marvell-prestera-armhf",
                                           "mellanox", "vs"]
    hotspot = result.brief.payload["hotspots"][0]
    assert hotspot["path"] == "device/arista/x86_64-arista_common/pmon_daemon_control.json"


def test_pmon_is_judged_per_family_through_the_symlink_it_travelled(tmp_path):
    result = _replay(PMON, tmp_path)
    question = result.agent.questions[0]
    families = sorted((group.families, len(group.members)) for group in question.assembly.groups)
    assert families == [(("barefoot",), 4), (("broadcom-dnx",), 10)]
    assert all(group.reach and group.reach[0].link.endswith("/pmon_daemon_control.json")
               for group in question.assembly.groups)
    finding = _finding(result, "materiality")
    assert finding["deterministic"]["statement"].endswith("(barefoot, broadcom-dnx).")
    assert {item["asic_families"][0] for item in finding["affected"]} == {"barefoot", "broadcom-dnx"}


def test_prestera_the_rule_decides_and_the_model_is_held_to_it(tmp_path):
    result = _replay(PRESTERA, tmp_path)
    finding = _finding(result, "ambiguity")
    candidate = finding["deterministic"]["rule_candidate"]
    assert (candidate["covered"], candidate["uncovered"]) == (5, 5)
    assert {item["id"]: item["resolution"] for item in finding["affected"]
            if item["id"].startswith("platform:marvell/x86_64")} == {
        "platform:marvell/x86_64-marvell_db98cx8514_10cc-r0": "uncovered",
        "platform:marvell/x86_64-marvell_db98cx8522_10cc-r0": "uncovered",
        "platform:marvell/x86_64-marvell_db98cx8540_16cd-r0": "uncovered",
        "platform:marvell/x86_64-marvell_db98cx8580_32cd-r0": "uncovered",
        "platform:marvell/x86_64-marvell_rd98DX35xx-r0": "uncovered",
    }
    answers = [answer for answer in finding["adjudication"]["answers"] if answer["group"]]
    assert {answer["group"]: (answer["rule"]["resolution"], answer["member_count"]) for answer in answers} == {
        "A": ("uncovered", 5), "B": ("covered", 4), "C": ("covered", 1)}
    for answer in answers:
        assert answer["outcome"] in ("confirmed", "contested", "dropped")
        if answer.get("covered") and answer["outcome"] != "dropped":
            assert answer["job_group"] in answer["rule"]["job_groups"]
