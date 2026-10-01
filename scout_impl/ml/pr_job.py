"""PR-grain job failure model, gated by fidelity and kept only if it beats the path heuristic.

The keep-or-discard decision reads validation alone; test PR-AUC is reported but never decides.
Rows whose diff fell back to two dots carry an empty path bag, so they are left out of training
and evaluation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from ..eval._io import load
from ..eval.calibration_rules import excluded_jobs
from ..eval.fidelity import check_coverage_fidelity
from ..eval.paths import eval_calibration_dir
from ..repos import get_adapter
from .risk import average_precision


_NUMERIC_FEATURE_KEYS = (
    "n_files",
    "additions",
    "deletions",
    "churn",
    "binary_files",
    "log1p_churn",
    "log1p_files",
    "n_shared_paths",
    "frac_shared",
    "max_revert_prior",
    "mean_revert_prior",
)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _job_names(rows: list[dict[str, Any]]) -> list[str]:
    return sorted({str(row["job"]) for row in rows})


def _vectorize(rows: list[dict[str, Any]], jobs: list[str]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    job_index = {name: index for index, name in enumerate(jobs)}
    vectors = []
    labels = []
    heuristics = []
    for row in rows:
        feat = row.get("features") or {}
        base = [float(feat.get(key) or 0) for key in _NUMERIC_FEATURE_KEYS]
        base.append(float(row.get("heuristic_p") or 0))
        job_vec = [0.0] * len(jobs)
        job_vec[job_index[str(row["job"])]] = 1.0
        vectors.append(base + job_vec)
        labels.append(int(row["y"]))
        heuristics.append(float(row.get("heuristic_p") or 0))
    return np.asarray(vectors, dtype=float), np.asarray(labels, dtype=int), np.asarray(heuristics, dtype=float)


def _split_rows(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    buckets: dict[str, list[dict[str, Any]]] = {"train": [], "valid": [], "test": []}
    for row in rows:
        split = row.get("split")
        if split in buckets and row.get("path_bag_usable") is not False:
            buckets[split].append(row)
    return buckets


def _fit(x: np.ndarray, y: np.ndarray, seed: int) -> Any:
    return make_pipeline(
        StandardScaler(),
        LogisticRegression(C=1.0, class_weight="balanced", max_iter=5000, random_state=seed),
    ).fit(x, y)


def train_pr_job(
    *,
    calibration_dir: Path | None,
    out: Path,
    repo_type: str,
    repo_root: Path,
    seed: int = 0,
    final: bool = False,
    cache_root: Path | None = None,
) -> dict[str, Any]:
    """Fit on train, decide on validation, report test. ``final`` refits a kept model on every split."""
    adapter = get_adapter(repo_type)
    if adapter is None:
        raise ValueError(f"unknown repo type {repo_type!r}")
    directory = calibration_dir or eval_calibration_dir(adapter, None, cache_root)
    timelines = load(str(directory), "azure-pr-job-timelines.json")
    cfg = adapter.calibration.tables if adapter.calibration else {}
    gold = set((cfg.get("jobs") or {}).get("gold") or [])
    azure_jobs: set[str] = set()
    for build in timelines.get("builds") or []:
        for group in (build.get("groupJobs") or {}).values():
            azure_jobs.update(group.keys())
    coverage = check_coverage_fidelity(gold, azure_jobs, excluded=set(excluded_jobs(cfg)))
    if not coverage["ok"]:
        raise ValueError(f"coverage fidelity failed: {coverage}")

    jsonl = directory / "model-dataset-pr-job.jsonl"
    if not jsonl.is_file():
        raise FileNotFoundError(f"missing {jsonl}; run mine-labels first")
    plan_path = directory / "scoring-plan.json"
    if not plan_path.is_file():
        raise ValueError("calibration cache must contain scoring-plan.json from mine-labels")

    rows = _load_jsonl(jsonl)
    if not rows:
        raise ValueError(f"{jsonl} is empty")
    jobs = _job_names(rows)
    by_split = _split_rows(rows)
    if not by_split["train"] or not by_split["valid"] or not by_split["test"]:
        raise ValueError("model-dataset-pr-job.jsonl must include train, valid and test splits")

    x_train, y_train, _ = _vectorize(by_split["train"], jobs)
    x_valid, y_valid, h_valid = _vectorize(by_split["valid"], jobs)
    x_test, y_test, h_test = _vectorize(by_split["test"], jobs)
    if y_train.sum() == 0 or y_valid.sum() == 0:
        raise ValueError("train or validation has no positive job failures")

    model = _fit(x_train, y_train, seed)
    valid_model = average_precision(y_valid, model.predict_proba(x_valid)[:, 1])
    valid_heuristic = average_precision(y_valid, h_valid)
    test_model = average_precision(y_test, model.predict_proba(x_test)[:, 1])
    test_heuristic = average_precision(y_test, h_test)
    beats = valid_model > valid_heuristic
    if beats and final:
        model = _fit(np.vstack([x_train, x_valid, x_test]), np.concatenate([y_train, y_valid, y_test]), seed)
    suffix = "-final" if final else ""

    out.mkdir(parents=True, exist_ok=True)
    card = {
        "repo_type": repo_type,
        "repo_root": str(repo_root),
        "calibration": str(directory),
        "rows": len(rows),
        "rows_by_split": {name: len(split_rows) for name, split_rows in by_split.items()},
        "rows_excluded_two_dot": sum(1 for row in rows if row.get("path_bag_usable") is False),
        "jobs": jobs,
        "fidelity": coverage,
        "metrics": {
            "validation": {"model_pr_auc": round(valid_model, 4), "heuristic_pr_auc": round(valid_heuristic, 4)},
            "test": {"model_pr_auc": round(test_model, 4), "heuristic_pr_auc": round(test_heuristic, 4)},
        },
        "selected": "logistic_pr_job" if beats else "heuristic_p_job",
        "beats_heuristic": beats,
        "gate": "validation PR-AUC above heuristic_p; test is reported, not used to decide",
        "final_refit": final,
    }
    card_path = out / f"pr-job-{repo_type}{suffix}.json"
    card_path.write_text(json.dumps(card, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if beats:
        joblib.dump(
            {"model": model, "jobs": jobs, "feature_keys": list(_NUMERIC_FEATURE_KEYS) + ["heuristic_p"],
             "final_refit": final},
            out / f"pr-job-{repo_type}{suffix}.joblib",
        )
    return card
