"""`review` end to end: the real static stage, the agent, the report, the four artifacts, and replay.

The tree here is small but shaped like the real one where stage 1 cares: both build stages
of the pipeline, a template the parser has to follow, arch-qualified job groups, and
`platform_asic` declarations in all three architectures, so the brief it produces has a
materiality question and an ambiguity question with a rule candidate, exactly as a real
review would.
"""

import json
import socket
from pathlib import Path

import pytest

from agent_world import ScriptedProvider, answers
from scout_impl.cli import run
from scout_impl.core.review import BRIEF_FILE, COMMENT_FILE, REPORT_FILE, RUN_LOG_FILE, review_fixture, run_review
from scout_impl.core.review_fixture import (
    BASE_TREE_FILE,
    HEAD_TREE_FILE,
    REPLAY_DIR,
    PairedFixtureSource,
    ReviewFixture,
    write_manifest,
)
from scout_impl.diffparse import parse_diff
from scout_impl.models import ChangeSet, ChangeSetSpec, CommitInfo
from scout_impl.provider import RecordingProvider, ReplayProvider
from scout_impl.repos import get_adapter
from scout_impl.static.fixtures import FixtureError, TreeFixture
from scout_impl.static.schema import validate_brief

HEAD = "a" * 40
BASE = "b" * 40
ADAPTER = get_adapter("sonic-buildimage")

PIPELINE = """stages:
- stage: BuildVS
  jobs:
  - template: .azure-pipelines/azure-pipelines-build.yml
    parameters:
      jobGroups:
      - name: vs
- stage: Build
  jobs:
  - template: .azure-pipelines/azure-pipelines-build.yml
    parameters:
      jobGroups:
      - name: broadcom
      - name: marvell-prestera-arm64
        variables:
          PLATFORM_NAME: marvell-prestera
          PLATFORM_ARCH: arm64
"""
TEMPLATE = """parameters:
- name: 'jobGroups'
  type: object
  default: []
jobs:
- ${{ each jobGroup in parameters.jobGroups }}:
  - job: ${{ jobGroup.name }}
"""
PMON = "device/acme/x86_64-acme_dnx-r0/pmon_daemon_control.json"
DIFF = f"""diff --git a/{PMON} b/{PMON}
index 1111111..2222222 100644
--- a/{PMON}
+++ b/{PMON}
@@ -1,3 +1,4 @@
 {{
-    "skip_fancontrol": true
+    "skip_fancontrol": true,
+    "delay_xcvrd": true
 }}
diff --git a/device/acme/x86_64-acme_pre-r0/platform_asic b/device/acme/x86_64-acme_pre-r0/platform_asic
index 3333333..4444444 100644
--- a/device/acme/x86_64-acme_pre-r0/platform_asic
+++ b/device/acme/x86_64-acme_pre-r0/platform_asic
@@ -1 +1 @@
-marvell
+marvell-prestera
diff --git a/device/acme/arm64-acme_pre-r0/platform_asic b/device/acme/arm64-acme_pre-r0/platform_asic
index 3333333..4444444 100644
--- a/device/acme/arm64-acme_pre-r0/platform_asic
+++ b/device/acme/arm64-acme_pre-r0/platform_asic
@@ -1 +1 @@
-marvell
+marvell-prestera
"""


def _files(head):
    prestera = "marvell-prestera\n" if head else "marvell\n"
    return {
        "azure-pipelines.yml": PIPELINE,
        ".azure-pipelines/azure-pipelines-build.yml": TEMPLATE,
        "slave.mk": "", "Makefile.work": "",
        "device/acme/x86_64-acme_dnx-r0/platform_asic": "broadcom-dnx\n",
        "device/acme/x86_64-acme_dnx-r0/pmon_daemon_control.json": (
            '{\n    "skip_fancontrol": true,\n    "delay_xcvrd": true\n}\n' if head
            else '{\n    "skip_fancontrol": true\n}\n'),
        "device/acme/x86_64-acme_dnx-r0/ACME-DNX/port_config.ini": "# name lanes\n",
        "device/acme/x86_64-acme_bcm-r0/platform_asic": "broadcom\n",
        "device/acme/x86_64-acme_pre-r0/platform_asic": prestera,
        "device/acme/arm64-acme_pre-r0/platform_asic": prestera,
    }


def _trees():
    head = TreeFixture.from_files(_files(True), repo="acme/buildimage", rev=HEAD, adapter=ADAPTER.name)
    base = TreeFixture.from_files(_files(False), repo="acme/buildimage", rev=BASE, adapter=ADAPTER.name)
    return head, base


def _change_set():
    commit = CommitInfo(sha=HEAD, parents=[BASE], subject="Delay xcvrd; rename marvell",
                        files=parse_diff(DIFF, ADAPTER))
    return ChangeSet(base_sha=BASE, head_sha=HEAD, spec=ChangeSetSpec(base_ref=BASE, head_ref=HEAD),
                     repo=ADAPTER.name, commits=[commit])


def _script():
    """One answer per question, in brief order: the ambiguity question, then materiality."""
    ambiguity = answers({"group": "A", "reason": "no amd64 group", "covered": False, "job_group": "",
                         "cite": ["E1", "E2", "E3"]},
                        {"group": "B", "reason": "arm64 group", "covered": True, "job_group": "marvell-prestera-arm64",
                         "cite": ["E1", "E5", "E6"]})
    materiality = answers({"group": "A", "reason": "xcvrd now starts late", "verdict": "material",
                           "cite": ["E1", "E3"]})
    return [ambiguity, materiality]


def _review(tmp_path, provider, name="out"):
    head, base = _trees()
    return run_review(PairedFixtureSource(head, base), repo="acme/buildimage", adapter=ADAPTER, rev=HEAD,
                      change_set=_change_set(), provider=provider, output_dir=tmp_path / name,
                      run_id="run-review-test", measured_at="2026-09-25T00:00:00Z")


def test_the_static_stage_puts_both_kinds_of_question_to_the_agent(tmp_path):
    result = _review(tmp_path, ScriptedProvider(_script()))
    questions = result.brief.payload["questions"]
    assert [question.get("kind") for question in questions] == ["ambiguity", "materiality"]
    assert [question.status for question in result.agent.questions] == ["answered", "answered"]


def test_a_review_writes_the_four_artifacts_and_each_validates(tmp_path):
    result = _review(tmp_path, ScriptedProvider(_script()))
    out = tmp_path / "out"
    assert sorted(path.name for path in out.iterdir()) == sorted([BRIEF_FILE, REPORT_FILE, COMMENT_FILE, RUN_LOG_FILE])
    validate_brief(json.loads((out / BRIEF_FILE).read_text(encoding="utf-8")))
    report = json.loads((out / REPORT_FILE).read_text(encoding="utf-8"))
    assert report["run"]["brief_sha"] == result.brief.sha()
    assert (out / COMMENT_FILE).read_text(encoding="utf-8") == result.comment
    events = [json.loads(line)["event"] for line in (out / RUN_LOG_FILE).read_text(encoding="utf-8").splitlines()]
    assert events[0] == "run_start" and events[-1] == "run_end"
    assert events.count("model_call") == 2
    assert {"stage_start", "stage_end", "question_start", "question_end"} <= set(events)


def test_the_brief_lands_even_when_stage_two_blows_up(tmp_path, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("stage 2 bug")

    monkeypatch.setattr("scout_impl.core.review.run_agent", explode)
    with pytest.raises(RuntimeError):
        _review(tmp_path, ScriptedProvider(_script()))
    assert (tmp_path / "out" / BRIEF_FILE).is_file()


def test_a_recorded_review_replays_to_the_same_report_with_no_network(tmp_path, monkeypatch):
    live = _review(tmp_path, RecordingProvider(ScriptedProvider(_script()), tmp_path / "replay"), name="live")

    def no_network(*args, **kwargs):
        raise AssertionError("a replayed review must not open a socket")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    replayed = _review(tmp_path, ReplayProvider.from_fixtures(tmp_path / "replay"), name="replayed")

    assert replayed.report.canonical_json() == live.report.canonical_json()
    assert replayed.comment == live.comment
    assert replayed.report.payload["run"]["model"]["replayed"] is True


def test_a_review_fixture_round_trips_through_its_manifest(tmp_path):
    folder = tmp_path / "fixture"
    folder.mkdir()
    head, base = _trees()
    head.write(folder / HEAD_TREE_FILE)
    base.write(folder / BASE_TREE_FILE)
    manifest = write_manifest(folder, _change_set(), repo="acme/buildimage", adapter=ADAPTER.name, mode="range",
                              run_id="run-review-test", measured_at="2026-09-25T00:00:00Z", base_tree=True)
    live = review_fixture(manifest, provider=RecordingProvider(ScriptedProvider(_script()), folder / REPLAY_DIR))

    fixture = ReviewFixture.load(manifest)
    assert (fixture.head.rev, fixture.base.rev, fixture.change_set.head_sha) == (HEAD, BASE, HEAD)
    replayed = review_fixture(manifest, provider=ReplayProvider.from_fixtures(fixture.replay_dir))
    assert replayed.report.canonical_json() == live.report.canonical_json()
    assert replayed.brief.canonical_json() == live.brief.canonical_json()
    assert (replayed.brief.id, replayed.brief.payload["brief"]["measured_at"]) == (
        "run-review-test", "2026-09-25T00:00:00Z")


def test_a_manifest_whose_trees_do_not_match_its_change_set_is_refused(tmp_path):
    head, base = _trees()
    head.write(tmp_path / HEAD_TREE_FILE)
    base.write(tmp_path / BASE_TREE_FILE)
    change_set = _change_set()
    swapped = ChangeSet(base_sha=HEAD, head_sha=BASE, spec=change_set.spec, repo=change_set.repo,
                        commits=change_set.commits)
    manifest = write_manifest(tmp_path, swapped, repo="r", adapter=ADAPTER.name, mode="range", run_id="x",
                              measured_at="2026-09-25T00:00:00Z", base_tree=True)
    with pytest.raises(FixtureError):
        ReviewFixture.load(manifest)


# --- the CLI ---------------------------------------------------------------------------------


def _cli(monkeypatch, *args):
    monkeypatch.setattr("sys.argv", ["run_scout.py", *args])
    return run()


def _fixture(tmp_path):
    head, base = _trees()
    head.write(tmp_path / HEAD_TREE_FILE)
    base.write(tmp_path / BASE_TREE_FILE)
    return write_manifest(tmp_path, _change_set(), repo="acme/buildimage", adapter=ADAPTER.name, mode="range",
                          run_id="run-cli", measured_at="2026-09-25T00:00:00Z", base_tree=True)


def test_review_with_no_provider_is_degraded_and_still_exits_zero(monkeypatch, tmp_path):
    manifest = _fixture(tmp_path)
    out = tmp_path / "out"
    assert _cli(monkeypatch, "review", "--fixture", str(manifest), "--provider", "none", "--output-dir", str(out)) == 0
    report = json.loads((out / REPORT_FILE).read_text(encoding="utf-8"))
    assert report["run"]["status"] == "degraded"
    assert report["run"]["model_calls"] == 0
    assert len(report["findings"]) == 2
    assert "Degraded" in (out / COMMENT_FILE).read_text(encoding="utf-8")


def test_review_replays_a_fixtures_own_recordings(monkeypatch, tmp_path):
    manifest = _fixture(tmp_path)
    review_fixture(manifest, provider=RecordingProvider(ScriptedProvider(_script()), tmp_path / REPLAY_DIR))
    out = tmp_path / "out"
    assert _cli(monkeypatch, "review", "--fixture", str(manifest), "--provider", "replay",
                "--output-dir", str(out)) == 0
    report = json.loads((out / REPORT_FILE).read_text(encoding="utf-8"))
    assert report["run"]["status"] == "complete"
    assert report["run"]["model"]["replayed"] is True


def test_review_replay_with_nothing_recorded_degrades_rather_than_failing(monkeypatch, tmp_path):
    manifest = _fixture(tmp_path)
    out = tmp_path / "out"
    assert _cli(monkeypatch, "review", "--fixture", str(manifest), "--provider", "replay",
                "--replay-dir", str(tmp_path / "empty"), "--output-dir", str(out)) == 0
    assert json.loads((out / REPORT_FILE).read_text(encoding="utf-8"))["run"]["status"] == "degraded"


def test_review_refuses_a_fixture_and_a_range_together(monkeypatch, tmp_path):
    manifest = _fixture(tmp_path)
    assert _cli(monkeypatch, "review", "--fixture", str(manifest), "--range", "a..b", "--provider", "none") == 1


def test_review_needs_a_change_set_to_review(monkeypatch, temp_repo):
    temp_repo.write("README.md", "x\n")
    temp_repo.commit("one")
    assert _cli(monkeypatch, "--repo-root", str(temp_repo.root), "review", "--provider", "none") == 1


def test_review_of_a_pull_request_needs_a_remote(monkeypatch, temp_repo):
    temp_repo.write("README.md", "x\n")
    temp_repo.commit("one")
    assert _cli(monkeypatch, "--repo-root", str(temp_repo.root), "review", "--pr", "24811", "--provider", "none") == 1


def test_review_of_a_plain_tree_fixture_reviews_the_whole_tree(monkeypatch, tmp_path):
    """`brief --fixture`'s input works here too: no change set, so materiality has nothing to judge."""
    upstream = Path(__file__).resolve().parent / "fixtures" / "trees" / "sonic-buildimage-master-62cfe50.json"
    out = tmp_path / "out"
    assert _cli(monkeypatch, "review", "--fixture", str(upstream), "--provider", "none", "--output-dir", str(out)) == 0
    report = json.loads((out / REPORT_FILE).read_text(encoding="utf-8"))
    assert report["run"]["mode"] == "tree" and report["run"]["status"] == "degraded"
    kinds = {finding["question"]: finding["deterministic"].get("rule_candidate") is not None
             for finding in report["findings"]}
    assert sorted(kinds.values()) == [False, True]
    brief = json.loads((out / BRIEF_FILE).read_text(encoding="utf-8"))
    assert report["findings"][0]["coverage_gap"]["platforms_in_tree"] == brief["coverage"]["platforms_in_tree"]


def test_review_is_listed_with_the_other_commands(monkeypatch, capsys):
    with pytest.raises(SystemExit):
        _cli(monkeypatch, "--help")
    assert "review" in capsys.readouterr().out
