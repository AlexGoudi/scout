"""PR-job trainer gates and beats-heuristic selection."""

import json
from pathlib import Path

import pytest

from scout_impl.ml.pr_job import train_pr_job


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _row(split: str, job: str, y: int, heuristic: float, churn: int) -> dict:
    return {
        "pr": 1,
        "sha": "abc",
        "split": split,
        "job": job,
        "y": y,
        "heuristic_p": heuristic,
        "features": {
            "n_files": 2,
            "churn": churn,
            "log1p_churn": 1.0,
            "log1p_files": 0.7,
            "max_revert_prior": 0.05,
            "mean_revert_prior": 0.03,
        },
        "path_bag_usable": True,
    }


@pytest.fixture
def calibration_dir(tmp_path: Path) -> Path:
    from scout_impl.repos.sonic_mgmt_calibration import JOIN_TABLES

    gold_jobs = JOIN_TABLES["jobs"]["gold"]
    directory = tmp_path / "cal"
    directory.mkdir()
    (directory / "scoring-plan.json").write_text('{"plan": {"gold_jobs": ["t0"]}}\n', encoding="utf-8")
    (directory / "azure-pr-job-timelines.json").write_text(
        json.dumps({"builds": [{"groupJobs": {"g": {job: {} for job in gold_jobs}}}]}) + "\n",
        encoding="utf-8",
    )
    rows = []
    for split, positives in (("train", 6), ("valid", 2), ("test", 2)):
        for index in range(20):
            y = 1 if index < positives else 0
            heuristic = 0.1 if y else 0.5
            churn = 100 if y else 5
            rows.append(_row(split, "t0", y, heuristic, churn))
    _write_jsonl(directory / "model-dataset-pr-job.jsonl", rows)
    return directory


def test_train_pr_job_beats_heuristic_on_synthetic(calibration_dir: Path, tmp_path: Path) -> None:
    card = train_pr_job(
        calibration_dir=calibration_dir,
        out=tmp_path / "models",
        repo_type="sonic-mgmt",
        repo_root=Path("/tmp/clone"),
        seed=0,
    )
    assert card["beats_heuristic"] is True
    assert card["selected"] == "logistic_pr_job"
    assert (tmp_path / "models" / "pr-job-sonic-mgmt.joblib").is_file()
