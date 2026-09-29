"""Commit risk baselines: train on train, select and calibrate on validation, report on test.

Four models, from least to most capable: the train prevalence, a size-only logistic
regression on log churn, a standardized logistic regression over every feature, and a
histogram gradient-boosting classifier. Hyperparameters come from a small fixed grid chosen
by validation PR-AUC, probabilities are Platt-calibrated on validation, and every test
metric carries a 1,000-sample bootstrap 95% interval. The same seed gives the same metrics.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np
from scipy.stats import rankdata

from .matrix import build_matrix, load_table

LABELS = ("bug_introducing", "reverted_within_90d")
MODEL_NAMES = ("prevalence", "size_only", "logistic", "gradient_boosting")
BOOTSTRAP_SAMPLES = 1000
EFFORT_SHARE = 0.20
LOGISTIC_GRID = (0.01, 0.1, 1.0)
BOOSTING_GRID = tuple(
    {"learning_rate": rate, "max_leaf_nodes": leaves} for rate in (0.05, 0.1) for leaves in (15, 31)
)
TOP_REASONS = 5
IMPORTANCE_REPEATS = 5
IMPORTANCE_TOP = 15


def average_precision(y: np.ndarray, scores: np.ndarray) -> float:
    """Step-wise PR-AUC with tied scores sharing one threshold, as scikit-learn computes it."""
    positives = y.sum()
    if positives == 0:
        return float("nan")
    order = np.argsort(-scores, kind="mergesort")
    ranked, sorted_scores = y[order], scores[order]
    ends = np.r_[np.nonzero(np.diff(sorted_scores))[0], len(sorted_scores) - 1]
    true_positives = np.cumsum(ranked)[ends]
    precision = true_positives / (ends + 1)
    recall = true_positives / positives
    return float(np.sum(np.diff(np.r_[0.0, recall]) * precision))


def roc_auc(y: np.ndarray, scores: np.ndarray) -> float:
    positives = y.sum()
    negatives = len(y) - positives
    if positives == 0 or negatives == 0:
        return float("nan")
    ranks = rankdata(scores)
    return float((ranks[y == 1].sum() - positives * (positives + 1) / 2) / (positives * negatives))


def recall_at_effort(y: np.ndarray, scores: np.ndarray, effort: np.ndarray, share: float = EFFORT_SHARE) -> float:
    """Share of positives found inspecting the riskiest commits until ``share`` of changed lines."""
    positives = y.sum()
    if positives == 0:
        return float("nan")
    order = np.argsort(-scores, kind="mergesort")
    inspected = np.cumsum(effort[order]) <= share * effort.sum()
    return float(y[order][inspected].sum() / positives)


def brier(y: np.ndarray, probabilities: np.ndarray) -> float:
    return float(np.mean((probabilities - y) ** 2))


METRICS: dict[str, Callable[..., float]] = {
    "pr_auc": lambda y, p, effort: average_precision(y, p),
    "roc_auc": lambda y, p, effort: roc_auc(y, p),
    "recall_at_20pct_effort": lambda y, p, effort: recall_at_effort(y, p, effort),
    "brier": lambda y, p, effort: brier(y, p),
}


def evaluate(
    y: np.ndarray, probabilities: np.ndarray, effort: np.ndarray, *, seed: int, samples: int = BOOTSTRAP_SAMPLES
) -> dict[str, dict[str, float | None]]:
    """Point estimates with percentile bootstrap 95% intervals over resampled rows."""
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, len(y), size=(samples, len(y))) if len(y) else np.empty((0, 0), dtype=int)
    result = {}
    for name, metric in METRICS.items():
        values = np.array([metric(y[rows], probabilities[rows], effort[rows]) for rows in draws])
        values = values[~np.isnan(values)]
        point = metric(y, probabilities, effort)
        result[name] = {
            "value": _round(point),
            "ci_low": _round(np.percentile(values, 2.5)) if len(values) else None,
            "ci_high": _round(np.percentile(values, 97.5)) if len(values) else None,
        }
    return result


@dataclass
class Platt:
    """Sigmoid calibration of a score, fitted on validation."""

    slope: float = 1.0
    intercept: float = 0.0

    @classmethod
    def fit(cls, scores: np.ndarray, y: np.ndarray) -> "Platt":
        from sklearn.linear_model import LogisticRegression

        if len(np.unique(y)) < 2:
            return cls()
        model = LogisticRegression(C=1e6, max_iter=1000).fit(_logit(scores)[:, None], y)
        return cls(float(model.coef_[0, 0]), float(model.intercept_[0]))

    def __call__(self, scores: np.ndarray) -> np.ndarray:
        return 1.0 / (1.0 + np.exp(-(self.slope * _logit(scores) + self.intercept)))


def train_risk(
    dataset: str | Path,
    label: str,
    out: str | Path,
    *,
    seed: int = 0,
    refresh: bool = False,
    final: bool = False,
) -> dict[str, Any]:
    """Train, select, calibrate and evaluate; write the model, its card and a report."""
    import joblib
    import sklearn
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.inspection import permutation_importance
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    if label not in LABELS:
        raise ValueError(f"label must be one of {LABELS}")
    dataset = Path(dataset)
    card_path = dataset / "dataset_card.json"
    card_sha = hashlib.sha256(card_path.read_bytes()).hexdigest() if card_path.is_file() else ""
    suffix = "-final" if final else ""
    input_hash = hashlib.sha256(f"{card_sha}:{label}:{seed}{suffix}".encode()).hexdigest()[:16]
    out_path = Path(out)
    model_file = out_path / f"risk-{label}{suffix}.joblib"
    model_card_file = out_path / f"risk-{label}{suffix}.model_card.json"
    if not refresh and model_file.is_file() and model_card_file.is_file():
        cached = json.loads(model_card_file.read_text(encoding="utf-8"))
        if cached.get("input_hash") == input_hash:
            return cached
    matrix = build_matrix(load_table(dataset), label)
    masks = {name: matrix.mask(name) for name in ("train", "validation", "test")}
    counts = {
        name: {"rows": int(mask.sum()), "positives": int(matrix.y[mask].sum())} for name, mask in masks.items()
    }
    if any(counts[name]["positives"] == 0 for name in ("train", "validation")):
        raise ValueError(f"{label} has no positives in train or validation: {counts}")
    x_train, y_train = matrix.x[masks["train"]], matrix.y[masks["train"]]
    x_valid, y_valid = matrix.x[masks["validation"]], matrix.y[masks["validation"]]
    churn_column = matrix.spec.names.index("churn")

    def size_features(x: np.ndarray) -> np.ndarray:
        return x[:, [churn_column]]

    fitted: dict[str, Any] = {}
    selection: dict[str, Any] = {}
    fitted["size_only"] = LogisticRegression(max_iter=1000).fit(size_features(x_train), y_train)

    best = None
    for c in LOGISTIC_GRID:
        model = make_pipeline(
            StandardScaler(), LogisticRegression(C=c, class_weight="balanced", max_iter=5000, random_state=seed)
        ).fit(x_train, y_train)
        score = average_precision(y_valid, model.predict_proba(x_valid)[:, 1])
        if best is None or score > best[0]:
            best = (score, c, model)
    assert best is not None
    fitted["logistic"] = best[2]
    selection["logistic"] = {"C": best[1], "validation_pr_auc": _round(best[0])}

    best = None
    for params in BOOSTING_GRID:
        model = HistGradientBoostingClassifier(
            max_iter=200, early_stopping=False, class_weight="balanced", random_state=seed, **params
        ).fit(x_train, y_train)
        score = average_precision(y_valid, model.predict_proba(x_valid)[:, 1])
        if best is None or score > best[0]:
            best = (score, params, model)
    assert best is not None
    fitted["gradient_boosting"] = best[2]
    selection["gradient_boosting"] = {**best[1], "validation_pr_auc": _round(best[0])}

    prevalence = float(y_train.mean())

    def raw(name: str, x: np.ndarray) -> np.ndarray:
        if name == "prevalence":
            return np.full(len(x), prevalence)
        if name == "size_only":
            return fitted[name].predict_proba(size_features(x))[:, 1]
        return fitted[name].predict_proba(x)[:, 1]

    calibrators = {name: Platt.fit(raw(name, x_valid), y_valid) for name in MODEL_NAMES if name != "prevalence"}

    def probability(name: str, x: np.ndarray) -> np.ndarray:
        scores = raw(name, x)
        return calibrators[name](scores) if name in calibrators else scores

    effort = matrix.effort
    validation_metrics = {}
    test_metrics = {}
    for position, name in enumerate(MODEL_NAMES):
        for split, target in (("validation", validation_metrics), ("test", test_metrics)):
            mask = masks[split]
            target[name] = evaluate(
                matrix.y[mask], probability(name, matrix.x[mask]), effort[mask], seed=seed + position
            )
    selected = max(
        ("logistic", "gradient_boosting"), key=lambda name: (validation_metrics[name]["pr_auc"]["value"] or 0, name)
    )

    importance = {}
    for name in ("logistic", "gradient_boosting"):
        result = permutation_importance(
            fitted[name], x_valid, y_valid, scoring="average_precision", n_repeats=IMPORTANCE_REPEATS,
            random_state=seed,
        )
        order = np.argsort(-result.importances_mean, kind="mergesort")[:IMPORTANCE_TOP]
        importance[name] = [
            {"feature": matrix.spec.names[i], "mean": _round(result.importances_mean[i]),
             "std": _round(result.importances_std[i])}
            for i in order
        ]

    labeled = np.ones(len(matrix.y), dtype=bool)
    reference = np.sort(probability(selected, matrix.x[labeled]))
    card = {
        "card_version": "1",
        "label": label,
        "seed": seed,
        "input_hash": input_hash,
        "final_refit": final,
        "dataset": {
            "path_name": dataset.name,
            "card_sha256": hashlib.sha256((dataset / "dataset_card.json").read_bytes()).hexdigest(),
            "snapshot": json.loads((dataset / "dataset_card.json").read_text())["snapshot"],
        },
        "rows": counts,
        "features": matrix.spec.names,
        "grid": {"logistic_C": list(LOGISTIC_GRID), "gradient_boosting": list(BOOSTING_GRID)},
        "selection": selection,
        "selected_model": selected,
        "calibration": {name: {"slope": _round(value.slope), "intercept": _round(value.intercept)}
                        for name, value in calibrators.items()},
        "validation_metrics": validation_metrics,
        "test_metrics": test_metrics,
        "permutation_importance": importance,
        "metric_notes": {
            "pr_auc": "average precision; the prevalence baseline's value is the positive rate",
            "recall_at_20pct_effort": "positives found inspecting the riskiest commits until 20% of changed "
            "lines are inspected; for the prevalence baseline this is landing order",
            "brier": "mean squared error of Platt-calibrated probabilities",
            "ci": f"percentile bootstrap over {BOOTSTRAP_SAMPLES} resamples of the split's rows",
        },
        "versions": {"scikit-learn": sklearn.__version__, "numpy": np.__version__},
    }

    out_path.mkdir(parents=True, exist_ok=True)
    if final:
        train_mask = matrix.mask("train") | matrix.mask("validation") | matrix.mask("test")
        fitted[selected].fit(matrix.x[train_mask], matrix.y[train_mask])
    joblib.dump(
        {
            "label": label,
            "spec": matrix.spec.to_dict(),
            "prevalence": prevalence,
            "models": fitted,
            "calibrators": {name: (value.slope, value.intercept) for name, value in calibrators.items()},
            "selected": selected,
            "reference_scores": reference,
            "card": card,
        },
        model_file,
    )
    model_card_file.write_text(json.dumps(card, indent=2, sort_keys=True) + "\n")
    (out_path / f"risk-{label}{suffix}.report.md").write_text(render_report(card))
    return card


def logistic_reasons(pipeline: Any, x_row: np.ndarray, names: Sequence[str], k: int = TOP_REASONS) -> list[dict]:
    """The ``k`` largest contributions (coefficient times standardized value) to one logit."""
    scaler, model = pipeline[0], pipeline[-1]
    contributions = model.coef_[0] * scaler.transform(x_row[None, :])[0]
    order = np.argsort(-np.abs(contributions), kind="mergesort")[:k]
    return [
        {"feature": names[i], "contribution": _round(contributions[i]), "direction": "raises" if contributions[i] > 0
         else "lowers"}
        for i in order
    ]


def load_model(path: str | Path) -> dict[str, Any]:
    import joblib

    bundle = joblib.load(path)
    if not isinstance(bundle, dict) or "spec" not in bundle:
        raise ValueError(f"{path} is not a risk model from 'ml train-risk'")
    return bundle


def predict(bundle: Mapping[str, Any], x: np.ndarray) -> np.ndarray:
    """Calibrated probabilities of the selected model."""
    name = bundle["selected"]
    slope, intercept = bundle["calibrators"][name]
    return Platt(slope, intercept)(bundle["models"][name].predict_proba(x)[:, 1])


def render_report(card: Mapping[str, Any]) -> str:
    rows = card["rows"]
    lines = [
        f"# Risk model: {card['label']}",
        "",
        f"Dataset snapshot `{card['dataset']['snapshot']}`, seed {card['seed']}. "
        f"Selected on validation PR-AUC: **{card['selected_model']}**.",
        "",
        "| Split | Rows | Positives |",
        "| --- | --- | --- |",
        *(f"| {name} | {value['rows']} | {value['positives']} |" for name, value in rows.items()),
        "",
        "## Test metrics (95% bootstrap interval)",
        "",
        "| Model | PR-AUC | ROC-AUC | Recall at 20% effort | Brier |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, metrics in card["test_metrics"].items():
        cells = [_interval(metrics[key]) for key in METRICS]
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines += ["", "## Permutation importance on validation (PR-AUC drop)", ""]
    for name, items in card["permutation_importance"].items():
        lines += [f"### {name}", "", "| Feature | Mean | Std |", "| --- | --- | --- |"]
        lines += [f"| {item['feature']} | {item['mean']} | {item['std']} |" for item in items]
        lines.append("")
    lines += ["## Notes", ""]
    lines += [f"- `{key}`: {value}" for key, value in card["metric_notes"].items()]
    lines.append("")
    return "\n".join(lines)


def _interval(metric: Mapping[str, Any]) -> str:
    if metric["value"] is None:
        return "n/a"
    if metric["ci_low"] is None:
        return f"{metric['value']:.3f}"
    return f"{metric['value']:.3f} [{metric['ci_low']:.3f}, {metric['ci_high']:.3f}]"


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def _round(value: float | None) -> float | None:
    if value is None or not np.isfinite(value):
        return None
    return round(float(value), 6)
