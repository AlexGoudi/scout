import json

import numpy as np
import pandas as pd
import pytest

from scout_impl.ml import online
from scout_impl.ml.matrix import column_spec


class ChurnModel:
    def predict_proba(self, x):
        p = np.clip(x[:, 0] / 10.0, 0.0, 1.0)
        return np.column_stack([1 - p, p])


def risk_bundle(frame, final=False):
    return {
        "label": "bug_introducing",
        "spec": column_spec(frame).to_dict(),
        "models": {"m": ChurnModel()},
        "calibrators": {"m": (1.0, 0.0)},
        "selected": "m",
        "card": {"label": "bug_introducing", "final_refit": final},
    }


def table():
    return pd.DataFrame(
        {
            "sha": ["a", "b", "c", "d"],
            "index": [0, 1, 2, 3],
            "split": ["train", "validation", "test", "test"],
            "change_type": ["code"] * 4,
            "is_merge": [False] * 4,
            "churn": [1, 2, 9, 3],
            "label_bug_introducing": [True, False, True, None],
        }
    )


def test_walk_forward_scores_only_held_out_rows_once(tmp_path, monkeypatch):
    frame = table()
    monkeypatch.setattr(online, "load_table", lambda _: frame)
    monkeypatch.setattr(online, "load_model", lambda _: risk_bundle(frame))
    ledger = tmp_path / "ledger.jsonl"
    summary = online.walk_forward("ds", "model", "sonic-buildimage", ledger=ledger)
    assert (summary["scored"], summary["appended"], summary["splits"]) == (2, 2, {"validation": 1, "test": 1})
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert [(row["subject"], row["outcome"]["y"]) for row in rows] == [("commit:b", 0), ("commit:c", 1)]
    assert online.walk_forward("ds", "model", "sonic-buildimage", ledger=ledger)["appended"] == 0
    card = online.grade_ledger("sonic-buildimage", calibration=tmp_path, ledger=ledger)
    graded = card["models"][summary["model"]]["graded"]
    assert (graded["n"], graded["positives"], graded["roc_auc"]) == (2, 1, 1.0)


def test_walk_forward_refuses_a_final_refit(tmp_path, monkeypatch):
    frame = table()
    monkeypatch.setattr(online, "load_table", lambda _: frame)
    monkeypatch.setattr(online, "load_model", lambda _: risk_bundle(frame, final=True))
    with pytest.raises(ValueError, match="--final"):
        online.walk_forward("ds", "model", "sonic-buildimage", ledger=tmp_path / "ledger.jsonl")


def test_watch_scores_each_head_once_and_grade_fills_azure_outcomes(tmp_path):
    prs = [{"number": 1, "head_sha": "h1", "title": "slave"}, {"number": 2, "head_sha": "h2", "title": "docs"}]
    files = {1: ["slave.mk", "rules/foo.mk"], 2: ["README.md"]}
    ledger = tmp_path / "ledger.jsonl"

    def watch():
        return online.watch_open_prs(
            "sonic-net/sonic-buildimage", "sonic-buildimage", calibration=tmp_path, ledger=ledger,
            list_prs=lambda owner, repo: prs, pr_files=lambda owner, repo, number: files[number],
        )

    first = watch()
    assert (first["open_prs"], first["appended"]) == (2, 2)
    assert watch()["appended"] == 0
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    jobs = sorted(rows[0]["score_by_job"])
    assert jobs and rows[0]["p"] >= rows[1]["p"]

    examples = [
        {"pr": 1, "finishTime": "2999-01-01T00:00:00Z", "azureBuildId": 7,
         "labels": {jobs[0]: "failed", **{job: "succeeded" for job in jobs[1:]}}},
        {"pr": 2, "finishTime": "2000-01-01T00:00:00Z", "azureBuildId": 3,
         "labels": {job: "succeeded" for job in jobs}},
    ]
    (tmp_path / "model-dataset-pr.json").write_text(json.dumps({"examples": examples}))
    card = online.grade_ledger("sonic-buildimage", calibration=tmp_path, ledger=ledger)
    assert card["newly_graded"] == 1
    model = card["models"][first["model"]]
    assert (model["rows"], model["pending"], model["graded"]["positives"]) == (2, 1, 1)
    assert model["job_grain"]["n"] == len(jobs)
    graded = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert graded[0]["outcome"]["azureBuildId"] == 7 and graded[1]["outcome"] is None
    assert online.grade_ledger("sonic-buildimage", calibration=tmp_path, ledger=ledger)["newly_graded"] == 0
