"""Chronological split calibration for detector prior and posting threshold (HLD 4.9, FR-10)."""

from __future__ import annotations


def pick_threshold_from_valid(scores: list[float], labels: list[int], grid: list[float] | None = None) -> dict:
    """
    Fit posting threshold on the validation split only.
    scores/labels must be same length; labels are 0/1 Azure Build failures.
    """
    grid = grid or [i / 100 for i in range(5, 96, 5)]
    best = {"threshold": 0.5, "f1": 0.0}
    for t in grid:
        tp = fp = fn = 0
        for s, y in zip(scores, labels):
            pred = 1 if s >= t else 0
            if pred == 1 and y == 1:
                tp += 1
            elif pred == 1 and y == 0:
                fp += 1
            elif pred == 0 and y == 1:
                fn += 1
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
        if f1 >= best["f1"]:
            best = {"threshold": t, "f1": round(f1, 4), "precision": round(prec, 4), "recall": round(rec, 4)}
    return best


def scorecard_on_test(predictions: list[int], labels: list[int]) -> dict:
    """Report held-out test metrics only (no tuning)."""
    tp = sum(1 for p, y in zip(predictions, labels) if p == 1 and y == 1)
    fp = sum(1 for p, y in zip(predictions, labels) if p == 1 and y == 0)
    fn = sum(1 for p, y in zip(predictions, labels) if p == 0 and y == 1)
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": round(prec, 4), "recall": round(rec, 4)}
