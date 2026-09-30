"""Phase-0 score-pr and the Azure label rules it shares with the model-* join."""

import json
from pathlib import Path

from scout_impl.eval.azure_dumps import join_cfg, select_attempts
from scout_impl.eval.calibration_rules import aggregate_attempt_labels, heuristic_p
from scout_impl.eval.score_pr import score_from_brief, score_paths, score_pr_repo
from scout_impl.repos import get_adapter

from conftest import TempGitRepo


def test_score_pr_diffs_from_the_merge_base_not_the_base_tip(temp_repo: TempGitRepo) -> None:
    temp_repo.write("README.md", "x\n")
    temp_repo.commit("init", date="2026-01-01T00:00:00+00:00")
    temp_repo.git("branch", "upstream")
    temp_repo.write("tests/conftest.py", "a = 1\n")
    temp_repo.commit("pr change", date="2026-01-02T00:00:00+00:00")
    temp_repo.git("checkout", "-q", "upstream")
    temp_repo.write("ansible/library/upstream_only.py", "b = 2\n")
    temp_repo.commit("upstream moved on", date="2026-01-03T00:00:00+00:00")
    temp_repo.git("checkout", "-q", "-")

    out = score_pr_repo(temp_repo.as_repo(), get_adapter("sonic-mgmt"), "upstream")

    assert out["files"] == ["tests/conftest.py"]


def test_score_paths_matches_the_model_heuristic_per_job(tmp_path: Path) -> None:
    adapter = get_adapter("sonic-mgmt")
    cfg = join_cfg(adapter)
    job_base = {job: 0.01 * (index + 1) for index, job in enumerate(cfg["jobs"]["gold"])}
    plan = {
        "path_weights": {"file_priors": {"tests/bgp/test_bgp.py": 0.05}, "base_rate_revert": 0.012},
        "job_base": job_base,
        "plan": {"gold_jobs": cfg["jobs"]["gold"]},
    }
    (tmp_path / "scoring-plan.json").write_text(json.dumps(plan), encoding="utf-8")
    paths = ["tests/bgp/test_bgp.py"]

    out = score_paths(paths, adapter, calibration_dir=tmp_path)

    files = [{"path": path} for path in paths]
    for job in cfg["jobs"]["gold"]:
        expected = heuristic_p(files, job, cfg, {"tests/bgp/test_bgp.py": 0.05}, 0.012, job_base[job])
        assert out["score_by_job"][job] == expected
    assert len(set(out["score_by_job"].values())) > 1
    assert out["job_base_source"] == "scoring-plan"


def test_paths_lift_jobs_whose_base_rate_exceeds_every_revert_prior() -> None:
    cfg = join_cfg(get_adapter("sonic-buildimage"))
    base, job_base = 0.013, 0.07
    priors = {"slave.mk": 0.026, "platform/mellanox/quiet.mk": 0.005}

    shared = heuristic_p([{"path": "slave.mk"}], "broadcom", cfg, priors, base, job_base)
    local = heuristic_p([{"path": "platform/mellanox/quiet.mk"}], "mellanox", cfg, priors, base, job_base)
    elsewhere = heuristic_p([{"path": "platform/mellanox/quiet.mk"}], "broadcom", cfg, priors, base, job_base)

    assert shared == round(job_base * (0.026 / base) * cfg["heuristic"]["shared_boost"], 6)
    assert local == elsewhere == job_base
    assert heuristic_p([{"path": "slave.mk"}] * 40, "vs", cfg, {"slave.mk": 0.5}, base, job_base) == 0.35


def test_brief_view_without_a_checkout_marks_files_incomplete(tmp_path: Path) -> None:
    brief = tmp_path / "scout-brief.json"
    brief.write_text(json.dumps({"hotspots": [{"path": "slave.mk"}]}), encoding="utf-8")

    out = score_from_brief(str(brief), get_adapter("sonic-buildimage"))

    assert out["files"] == ["slave.mk"]
    assert out["files_complete"] is False


def test_brief_view_with_a_checkout_scores_every_changed_file(temp_repo: TempGitRepo, tmp_path: Path) -> None:
    temp_repo.write("README.md", "x\n")
    base = temp_repo.commit("init", date="2026-01-01T00:00:00+00:00")
    for index in range(12):
        temp_repo.write(f"device/vendor/p{index}/platform_asic", "broadcom\n")
    head = temp_repo.commit("many files", date="2026-01-02T00:00:00+00:00")
    brief = tmp_path / "scout-brief.json"
    payload = {"brief": {"base_sha": base, "head_sha": head}, "hotspots": [{"path": "device/vendor/p0/platform_asic"}]}
    brief.write_text(json.dumps(payload), encoding="utf-8")

    out = score_from_brief(str(brief), get_adapter("sonic-buildimage"), repo=temp_repo.as_repo())

    assert len(out["files"]) == 12
    assert out["files_complete"] is True


def test_a_failure_on_any_attempt_labels_the_job_failed() -> None:
    attempts = [
        {"t0": "failed", "t2": "succeeded", "dpu": None},
        {"t0": "succeeded", "t2": "succeeded", "dpu": "canceled"},
    ]

    assert aggregate_attempt_labels(attempts) == {"t0": "failed", "t2": "succeeded", "dpu": "canceled"}


def test_select_attempts_caps_prs_and_attempts_per_pr() -> None:
    builds = [
        {"id": 6, "pr": 3},
        {"id": 5, "pr": 2},
        {"id": 4, "pr": 2},
        {"id": 3, "pr": 2},
        {"id": 2, "pr": 1},
        {"id": 1, "pr": None},
    ]

    picked = select_attempts(builds, max_prs=2, per_pr=2)

    assert [row["id"] for row in picked] == [6, 5, 4]
