"""Online evaluation ledger: out-of-sample replay, live PR watch, and grading.

One JSONL ledger per adapter. A row is keyed by ``(subject, model)`` and is never rewritten
except by ``grade``, which fills ``outcome`` once the Azure gold labels for that PR exist.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np

from ..eval.github_api import list_open_prs, list_pr_files
from ..eval.history import eval_cache_root
from ..eval.paths import eval_calibration_dir
from ..eval.score_pr import score_paths
from ..repos import get_adapter
from ..repos.base import RepoAdapter
from .matrix import build_matrix, load_table, spec_from_dict
from .risk import average_precision, load_model, predict, roc_auc

OUT_OF_SAMPLE = ("validation", "test")
GOLD_RESULTS = ("succeeded", "failed")


def _adapter(repo_type: str) -> RepoAdapter:
    adapter = get_adapter(repo_type)
    if adapter is None:
        raise ValueError(f"unknown repo type {repo_type!r}")
    return adapter


def _calibration(adapter: RepoAdapter, calibration: Path | None) -> Path:
    return calibration or eval_calibration_dir(adapter, None)


def ledger_path(repo_type: str) -> Path:
    return eval_cache_root(None) / "online" / _adapter(repo_type).name / "ledger.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _append(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    """Append rows whose ``(subject, model)`` is not in the ledger yet; the count appended."""
    seen = {(row.get("subject"), row.get("model")) for row in _read(path)}
    fresh = []
    for row in rows:
        key = (row["subject"], row["model"])
        if key not in seen:
            seen.add(key)
            fresh.append(row)
    if fresh:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for row in fresh:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
    return len(fresh)


def walk_forward(dataset: str, model_path: str, repo_type: str, ledger: Path | None = None) -> dict:
    """Score the validation and test commits the model never fit, with the label they carry.

    A ``--final`` model was refit on every split, so nothing it could score is out of sample.
    """
    bundle = load_model(model_path)
    card = bundle["card"]
    if card.get("final_refit"):
        raise ValueError(f"{model_path} was refit on every split (--final); walk-forward needs a held-out model")
    card_sha = hashlib.sha256(json.dumps(card, sort_keys=True).encode()).hexdigest()[:16]
    label = bundle["label"]
    matrix = build_matrix(load_table(Path(dataset)), label, spec_from_dict(bundle["spec"]))
    keep = matrix.frame["split"].isin(OUT_OF_SAMPLE).to_numpy()
    frame = matrix.frame.loc[keep]
    scores = predict(bundle, matrix.x[keep]) if keep.any() else np.empty(0)
    labels = matrix.y[keep]
    scored_at = _now()
    rows = [
        {
            "subject": f"commit:{sha}",
            "model": f"risk-{label}:{card_sha}",
            "kind": "commit",
            "scored_at": scored_at,
            "split": split,
            "p": float(score),
            "label": label,
            "outcome": {"y": int(y)},
            "outcome_source": "dataset-snapshot",
        }
        for sha, split, score, y in zip(frame["sha"].astype(str), frame["split"].astype(str), scores, labels)
    ]
    path = ledger or ledger_path(repo_type)
    return {
        "ledger": str(path),
        "repo_type": repo_type,
        "model": f"risk-{label}:{card_sha}",
        "scored": len(rows),
        "appended": _append(path, rows),
        "splits": {split: int((frame["split"] == split).sum()) for split in OUT_OF_SAMPLE},
    }


def _plan_sha(calibration: Path) -> str:
    plan = calibration / "scoring-plan.json"
    if not plan.is_file():
        return "adapter-defaults"
    return hashlib.sha256(plan.read_bytes()).hexdigest()[:16]


def watch_open_prs(
    remote: str,
    repo_type: str,
    calibration: Path | None = None,
    max_prs: int = 100,
    ledger: Path | None = None,
    list_prs: Callable[[str, str], list[dict]] = list_open_prs,
    pr_files: Callable[[str, str, int], list[str]] = list_pr_files,
) -> dict:
    """Score each open PR head once with the phase-0 path heuristic, before Azure has an answer."""
    owner, repo = remote.split("/")[-2:]
    adapter = _adapter(repo_type)
    calibration = _calibration(adapter, calibration)
    model = f"phase0:{_plan_sha(calibration)}"
    path = ledger or ledger_path(repo_type)
    seen = {(row.get("subject"), row.get("model")) for row in _read(path)}
    open_prs = list_prs(owner, repo)
    rows = []
    for pr in open_prs:
        if len(rows) >= max_prs:
            break
        subject = f"pr:{pr['number']}:{pr['head_sha']}"
        if (subject, model) in seen:
            continue
        paths = pr_files(owner, repo, pr["number"])
        scored = score_paths(paths, adapter, base_ref="pr-files", calibration_dir=calibration)
        by_job = scored["score_by_job"]
        rows.append(
            {
                "subject": subject,
                "model": model,
                "kind": "pr",
                "scored_at": _now(),
                "pr": pr["number"],
                "head_sha": pr["head_sha"],
                "title": pr.get("title"),
                "n_files": len(paths),
                "score_by_job": by_job,
                "p": max(by_job.values(), default=None),
                "outcome": None,
            }
        )
    return {
        "ledger": str(path),
        "repo_type": repo_type,
        "model": model,
        "open_prs": len(open_prs),
        "appended": _append(path, rows),
    }


def _pr_outcomes(calibration: Path) -> dict[int, list[dict[str, Any]]]:
    path = calibration / "model-dataset-pr.json"
    if not path.is_file():
        return {}
    examples = json.loads(path.read_text(encoding="utf-8")).get("examples") or []
    by_pr: dict[int, list[dict[str, Any]]] = {}
    for example in examples:
        by_pr.setdefault(int(example["pr"]), []).append(example)
    return by_pr


def _outcome_for(row: dict[str, Any], outcomes: dict[int, list[dict[str, Any]]]) -> dict[str, Any] | None:
    """Gold labels of the first Azure build of this PR that finished after the row was scored."""
    after = [
        example for example in outcomes.get(int(row["pr"]), [])
        if (example.get("finishTime") or "") >= row["scored_at"][:19]
    ]
    if not after:
        return None
    example = min(after, key=lambda item: item.get("finishTime") or "")
    labels = {job: result for job, result in (example.get("labels") or {}).items() if result in GOLD_RESULTS}
    if not labels:
        return None
    return {
        "y": int(any(result == "failed" for result in labels.values())),
        "labels": labels,
        "azureBuildId": example.get("azureBuildId"),
    }


def _metrics(pairs: list[tuple[float, int]]) -> dict[str, Any]:
    y = np.array([label for _, label in pairs], dtype=float)
    scores = np.array([score for score, _ in pairs], dtype=float)

    def clean(value: float) -> float | None:
        return None if math.isnan(value) else round(value, 4)

    return {
        "n": len(pairs),
        "positives": int(y.sum()),
        "roc_auc": clean(roc_auc(y, scores)) if pairs else None,
        "pr_auc": clean(average_precision(y, scores)) if pairs else None,
    }


def grade_ledger(repo_type: str, calibration: Path | None = None, ledger: Path | None = None) -> dict:
    """Fill PR outcomes from the calibration join, then score every model on its graded rows."""
    adapter = _adapter(repo_type)
    calibration = _calibration(adapter, calibration)
    path = ledger or ledger_path(repo_type)
    rows = _read(path)
    outcomes = _pr_outcomes(calibration)
    newly = 0
    for row in rows:
        if row.get("kind") == "pr" and row.get("outcome") is None:
            outcome = _outcome_for(row, outcomes)
            if outcome is not None:
                row["outcome"] = outcome
                row["outcome_source"] = "azure-gold-jobs"
                row["graded_at"] = _now()
                newly += 1
    if newly:
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
        tmp.replace(path)

    models: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = models.setdefault(row["model"], {"rows": 0, "pending": 0, "unit": [], "job": []})
        entry["rows"] += 1
        outcome = row.get("outcome")
        if outcome is None or row.get("p") is None:
            entry["pending"] += 1
            continue
        entry["unit"].append((float(row["p"]), int(outcome["y"])))
        for job, result in (outcome.get("labels") or {}).items():
            score = (row.get("score_by_job") or {}).get(job)
            if score is not None:
                entry["job"].append((float(score), int(result == "failed")))
    scorecard = {
        "repo_type": repo_type,
        "ledger": str(path),
        "rows": len(rows),
        "newly_graded": newly,
        "generated_at": _now(),
        "models": {
            name: {
                "rows": entry["rows"],
                "pending": entry["pending"],
                "graded": _metrics(entry["unit"]),
                **({"job_grain": _metrics(entry["job"])} if entry["job"] else {}),
            }
            for name, entry in sorted(models.items())
        },
        "notes": {
            "graded": "commit rows: label as of the dataset snapshot; pr rows: any gold job failed",
            "job_grain": "pr rows only: score_by_job[job] against that job's Azure result",
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    (path.parent / "online-scorecard.json").write_text(json.dumps(scorecard, indent=2, sort_keys=True) + "\n")
    return scorecard
