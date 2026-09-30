"""Backtest D6 over the seed corpus: recall on incidents, flag rate on controls (HLD section 6.3).

Each corpus item is analyzed at its cause, `parent..cause`, from a pinned per-item fixture.
An incident is recalled when the platforms the static stage reports as never built include
one the revert names (`groundtruth.match`); a control is a false positive when the stage
reports any never-built platform at all, because a control was never reverted. Both
coverage readings are scored: string equality (uncovered plus ambiguous) and
architecture-aware (uncovered only).

The first run needs the network: it mines the corpus and captures one fixture per item.
Every later run replays the pinned corpus and fixtures with no git and no network.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..repos import get_adapter
from ..static.fixtures import FixtureError
from .corpus import ADAPTER_NAME, CORPUS_VERSION, MANIFEST_FILE, Corpus, CorpusRules, build_corpus, load_corpus
from .fixtures import ItemFixture, capture_item, coverage_sets, replay_item
from .groundtruth import GroundTruth, match
from .history import eval_cache_root, open_history

logger = logging.getLogger(__name__)

BACKTEST_SUBDIR = "backtest"
SCORECARD_FILE = "scorecard.json"
READINGS = {
    "string_equality": ("uncovered", "ambiguous"),
    "architecture_aware": ("uncovered",),
}


class BacktestError(RuntimeError):
    """The backtest cannot run as asked: no corpus offline, or an adapter it does not grade."""


def corpus_dir(cache_root: Path | None, adapter_name: str) -> Path:
    return eval_cache_root(cache_root) / BACKTEST_SUBDIR / adapter_name


def wilson(successes: int, total: int, z: float = 1.96) -> list[float] | None:
    """95% Wilson score interval; stays inside [0, 1] and is honest at small n."""
    if total == 0:
        return None
    p = successes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def _rate(successes: int, total: int) -> dict[str, Any]:
    return {
        "n": total,
        "k": successes,
        "rate": round(successes / total, 4) if total else None,
        "ci95": wilson(successes, total),
    }


def _load_or_build_corpus(directory: Path, capture: bool, cache_root: Path | None, revision: str | None) -> Corpus:
    if (directory / MANIFEST_FILE).is_file():
        return load_corpus(directory)
    if not capture:
        raise BacktestError(
            f"No pinned corpus at {directory}. Run `backtest --capture` once with network access to mine the "
            "corpus and capture its fixtures; later runs replay them offline."
        )
    history = open_history(cache_root=cache_root, revision=revision)
    corpus = build_corpus(history, CorpusRules())
    corpus.write(directory)
    return corpus


def _item_run(corpus: Corpus, item: dict, adapter, capture: bool, cache_root: Path | None):
    path = corpus.fixture_path(item["item_id"])
    if path.is_file():
        return replay_item(ItemFixture.load(path), adapter), "replayed"
    if not capture:
        return None, "missing_fixture"
    fixture, run = capture_item(item["item_id"], item["cause_sha"], adapter, cache_root=cache_root)
    fixture.write(path)
    return run, "captured"


def grade_item(item: dict, coverage: dict[str, list[str]], families: dict[str, tuple[str, ...]]) -> dict:
    """One item's outcome under both readings. Pure: the brief's coverage sets in, a row out."""
    row: dict[str, Any] = {
        "item_id": item["item_id"],
        "kind": item["kind"],
        "cause_sha": item["cause_sha"],
        "affected": len(coverage.get("affected") or []),
    }
    truth = GroundTruth.from_dict(item["ground_truth"]) if item.get("ground_truth") else None
    for reading, keys in READINGS.items():
        flagged = sorted({entity for key in keys for entity in coverage.get(key) or []})
        outcome: dict[str, Any] = {"flagged": len(flagged)}
        if truth is not None:
            graded = match(truth, flagged, families)
            outcome["recalled"] = graded.recalled
            outcome["level"] = graded.level
        row[reading] = outcome
    return row


def summarize(rows: list[dict]) -> dict[str, Any]:
    incidents = [row for row in rows if row["kind"] == "incident"]
    controls = [row for row in rows if row["kind"] == "control"]
    out: dict[str, Any] = {}
    for reading in READINGS:
        recalled = sum(1 for row in incidents if row[reading].get("recalled"))
        flagged_incidents = sum(1 for row in incidents if row[reading]["flagged"])
        flagged_controls = sum(1 for row in controls if row[reading]["flagged"])
        posted = flagged_incidents + flagged_controls
        out[reading] = {
            "recall": _rate(recalled, len(incidents)),
            "control_flag_rate": _rate(flagged_controls, len(controls)),
            "precision_on_this_corpus": _rate(recalled, posted),
        }
    return out


def run_backtest(
    adapter_name: str = ADAPTER_NAME,
    cache_root: Path | None = None,
    capture: bool = False,
    directory: Path | None = None,
    revision: str | None = None,
) -> dict:
    if adapter_name != ADAPTER_NAME:
        raise BacktestError(
            f"The seed corpus grades D6 on {ADAPTER_NAME} platform coverage; {adapter_name} has no corpus. "
            "Use the Azure-labelled model-* join (`ml train-pr-job`) to evaluate it instead."
        )
    adapter = get_adapter(adapter_name)
    directory = Path(directory) if directory is not None else corpus_dir(cache_root, adapter_name)
    corpus = _load_or_build_corpus(directory, capture, cache_root, revision)

    rows: list[dict] = []
    status: dict[str, int] = {"replayed": 0, "captured": 0, "missing_fixture": 0, "failed": 0}
    for item in corpus.items:
        try:
            run, how = _item_run(corpus, item, adapter, capture, cache_root)
        except (FixtureError, OSError, RuntimeError) as error:
            logger.warning("backtest: %s failed: %s", item["item_id"], error)
            status["failed"] += 1
            rows.append({"item_id": item["item_id"], "kind": item["kind"], "error": str(error)})
            continue
        status[how] += 1
        if run is None:
            continue
        index = run.result.index
        families = {entity.id: tuple(entity.families) for entity in index.entities} if index is not None else {}
        rows.append(grade_item(item, coverage_sets(run.brief), families))

    graded = [row for row in rows if "error" not in row]
    scorecard = {
        "adapter": adapter_name,
        "corpus_version": CORPUS_VERSION,
        "corpus_dir": str(directory),
        "history_tip": (corpus.manifest.get("history") or {}).get("revision", "")[:12],
        "adjudication": corpus.manifest.get("adjudication"),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "items": {"incidents": len(corpus.incidents), "controls": len(corpus.controls), **status},
        "metrics": summarize(graded),
        "note": (
            "Recall: an incident's never-built set names a platform, family or vendor its revert names. "
            "Control flag rate: a never-reverted control got any never-built finding. Precision depends on "
            "the corpus's incident/control mix, so quote it only beside that mix. No bare percentages: "
            "every rate carries its Wilson 95% interval."
        ),
        "rows": rows,
    }
    if graded:
        (directory / SCORECARD_FILE).write_text(json.dumps(scorecard, indent=2, sort_keys=True) + "\n",
                                                encoding="utf-8")
    return scorecard
