import json

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sklearn.metrics import average_precision_score, roc_auc_score

import run_scout
from scout_impl.ml.matrix import build_matrix, load_table
from scout_impl.ml.risk import (
    MODEL_NAMES,
    average_precision,
    evaluate,
    load_model,
    logistic_reasons,
    predict,
    recall_at_effort,
    roc_auc,
    train_risk,
)


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_metrics_agree_with_scikit_learn_including_ties(seed):
    rng = np.random.default_rng(seed)
    y = (rng.random(400) < 0.2).astype(float)
    scores = np.round(rng.random(400) + y * 0.3, 1)
    assert average_precision(y, scores) == pytest.approx(average_precision_score(y, scores))
    assert roc_auc(y, scores) == pytest.approx(roc_auc_score(y, scores))


def test_recall_at_effort_inspects_the_riskiest_commits_first():
    y = np.array([1, 0, 1, 0], dtype=float)
    scores = np.array([0.9, 0.8, 0.1, 0.2])
    effort = np.array([10, 10, 10, 70], dtype=float)
    assert recall_at_effort(y, scores, effort, share=0.2) == 0.5
    assert recall_at_effort(y, scores, effort, share=1.0) == 1.0


def test_bootstrap_intervals_are_seeded_and_bracket_the_point():
    rng = np.random.default_rng(0)
    y = (rng.random(300) < 0.3).astype(float)
    p = np.clip(0.3 + 0.3 * (y - 0.3) + rng.normal(0, 0.1, 300), 0.01, 0.99)
    first = evaluate(y, p, np.ones(300), seed=4, samples=200)
    assert first == evaluate(y, p, np.ones(300), seed=4, samples=200)
    for metric in first.values():
        assert metric["ci_low"] <= metric["value"] <= metric["ci_high"]


def synthetic_dataset(directory, rows=3000):
    rng = np.random.default_rng(7)
    churn = rng.integers(1, 2000, rows)
    fixes = rng.integers(0, 20, rows)
    entropy = rng.random(rows) * 3
    days = np.where(rng.random(rows) < 0.2, np.nan, rng.random(rows) * 300)
    logit = -3 + 0.5 * np.log1p(churn) - 0.8 * entropy + 0.1 * fixes + rng.normal(0, 0.5, rows)
    label = rng.random(rows) < 1 / (1 + np.exp(-logit))
    splits = np.array(["train"] * int(rows * 0.7) + ["validation"] * int(rows * 0.15))
    splits = np.r_[splits, ["test"] * (rows - len(splits))]
    splits[10] = "gap"
    frame = pd.DataFrame(
        {
            "sha": [f"{index:040x}" for index in range(rows)],
            "index": np.arange(rows),
            "landed": np.arange(rows) * 86_400,
            "split": splits,
            "change_type": np.where(fixes > 10, "fix", "feature"),
            "is_merge": np.arange(rows) == 5,
            "churn": churn,
            "entropy": entropy,
            "file_prior_fix_touches": fixes,
            "is_doc_only": rng.random(rows) < 0.05,
            "area_revert_rate": rng.random(rows) * 0.05,
            "file_days_since_last_change": days,
            "label_bug_introducing": pd.array(label, dtype="boolean"),
            "label_reverted_within_90d": pd.array(label & (rng.random(rows) < 0.3), dtype="boolean"),
        }
    )
    frame.loc[7, "label_bug_introducing"] = pd.NA
    directory.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), directory / "features.parquet")
    (directory / "dataset_card.json").write_text(json.dumps({"snapshot": "f" * 40}))
    return directory


def test_matrix_drops_merges_gaps_and_unknown_labels(tmp_path):
    frame = load_table(synthetic_dataset(tmp_path / "data"))
    matrix = build_matrix(frame, "bug_introducing")
    assert len(matrix.y) == len(frame) - 3
    names = matrix.spec.names
    assert "file_days_since_last_change__missing" in names and "change_type=fix" in names
    churn = matrix.x[:, names.index("churn")]
    assert churn.max() == pytest.approx(np.log1p(frame["churn"].max()))
    assert "is_merge" not in names and not any(name.startswith("label_") for name in names)


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    root = tmp_path_factory.mktemp("risk")
    dataset = synthetic_dataset(root / "data")
    card = train_risk(dataset, "bug_introducing", root / "models", seed=0)
    return root, dataset, card


def test_training_reports_every_model_with_intervals(trained):
    root, dataset, card = trained
    assert set(card["test_metrics"]) == set(MODEL_NAMES)
    for metrics in card["test_metrics"].values():
        assert set(metrics) == {"pr_auc", "roc_auc", "recall_at_20pct_effort", "brier"}
        assert all(value["ci_low"] is not None for value in metrics.values())
    prevalence = card["test_metrics"]["prevalence"]["pr_auc"]["value"]
    assert card["test_metrics"]["logistic"]["pr_auc"]["value"] > prevalence + 0.1
    assert card["selected_model"] in ("logistic", "gradient_boosting")
    for suffix in ("joblib", "model_card.json", "report.md"):
        assert (root / "models" / f"risk-bug_introducing.{suffix}").is_file()


def test_retraining_gives_identical_metrics(trained, tmp_path):
    root, dataset, card = trained
    again = train_risk(dataset, "bug_introducing", tmp_path, seed=0)
    assert again == card
    assert (tmp_path / "risk-bug_introducing.model_card.json").read_bytes() == (
        root / "models" / "risk-bug_introducing.model_card.json"
    ).read_bytes()


def test_saved_model_predicts_and_explains(trained):
    root, dataset, card = trained
    bundle = load_model(root / "models" / "risk-bug_introducing.joblib")
    matrix = build_matrix(load_table(dataset), "bug_introducing")
    probabilities = predict(bundle, matrix.x[:50])
    assert probabilities.shape == (50,) and ((probabilities > 0) & (probabilities < 1)).all()
    reasons = logistic_reasons(bundle["models"]["logistic"], matrix.x[0], matrix.spec.names)
    assert len(reasons) == 5 and {reason["direction"] for reason in reasons} <= {"raises", "lowers"}


def test_cli_trains(trained, tmp_path, capsys):
    root, dataset, card = trained
    arguments = ["ml", "train-risk", "--dataset", str(dataset), "--label", "reverted_within_90d",
                 "--out", str(tmp_path)]
    assert run_scout.main(arguments) == 0
    assert set(json.loads(capsys.readouterr().out)) == set(MODEL_NAMES)
