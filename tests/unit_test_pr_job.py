"""PR-job trainer gates and beats-heuristic selection."""

import json
from pathlib import Path

import joblib
import pytest

from scout_impl.ml.online import walk_forward
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


def _rewrite(directory: Path, edit) -> None:
    path = directory / "model-dataset-pr-job.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    _write_jsonl(path, edit(rows))


def _train(calibration_dir: Path, out: Path, **kwargs) -> dict:
    return train_pr_job(
        calibration_dir=calibration_dir, out=out, repo_type="sonic-mgmt", repo_root=Path("/tmp/clone"), **kwargs
    )


def test_gate_reads_validation_only(calibration_dir: Path, tmp_path: Path) -> None:
    def invert_test(rows):
        for row in rows:
            if row["split"] == "test":
                row["features"]["churn"] = 5 if row["y"] else 100
                row["heuristic_p"] = 0.5 if row["y"] else 0.1
        return rows

    _rewrite(calibration_dir, invert_test)
    card = _train(calibration_dir, tmp_path / "models")
    test = card["metrics"]["test"]
    assert test["model_pr_auc"] < test["heuristic_pr_auc"]
    assert card["beats_heuristic"] is True and card["selected"] == "logistic_pr_job"


def test_two_dot_rows_are_left_out(calibration_dir: Path, tmp_path: Path) -> None:
    def add_two_dot(rows):
        extra = [dict(_row("train", "t0", 1, 0.9, 0), path_bag_usable=False) for _ in range(15)]
        return rows + extra

    _rewrite(calibration_dir, add_two_dot)
    card = _train(calibration_dir, tmp_path / "models")
    assert card["rows_excluded_two_dot"] == 15
    assert card["rows_by_split"] == {"train": 20, "valid": 20, "test": 20}
    assert card["beats_heuristic"] is True


def test_final_refits_a_kept_model_under_its_own_name(calibration_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "models"
    card = _train(calibration_dir, out, final=True)
    assert card["final_refit"] is True and card["beats_heuristic"] is True
    assert (out / "pr-job-sonic-mgmt-final.json").is_file()
    assert not (out / "pr-job-sonic-mgmt.json").exists()
    bundle = joblib.load(out / "pr-job-sonic-mgmt-final.joblib")
    assert bundle["final_refit"] is True
    with pytest.raises(ValueError):
        walk_forward("unused", str(out / "pr-job-sonic-mgmt-final.joblib"), "sonic-mgmt", ledger=tmp_path / "l.jsonl")
