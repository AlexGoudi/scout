"""An LLM-ready bundle for one commit: its label-free document, a calibrated risk with the
logistic regression's top reasons, and similar earlier commits with their outcomes as known
when the commit landed.

A commit on the dataset's first-parent chain uses its own ``features.parquet`` row. Any other
commit is mined from the clone and scored as if it landed right after the snapshot, with
history features computed from the dataset's commits plus this one.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from ..dataset.categories import categorize
from ..dataset.export import LABEL_DEFINITIONS, feature_row, intrinsic_columns, llm_document
from ..dataset.history import CommitFacts, facts_from_record, history_features
from ..dataset.labels import link_reverts
from ..mining.gitio import Git
from ..mining.message import DEFAULT_AUTHOR_SALT
from ..mining.record import make_context, mine_commit, record_to_json, utc
from .matrix import load_table, spec_from_dict
from .risk import load_model, logistic_reasons, predict
from .similar import SimilarityIndex

BUNDLE_VERSION = "1"
ONE_HOT_PREFIXES = ("component__", "area__")


def score_commit(
    dataset: str | Path,
    model: str | Path,
    repo: str | Path,
    commit: str,
    *,
    k: int = 5,
    salt: str = DEFAULT_AUTHOR_SALT,
) -> dict[str, Any]:
    dataset = Path(dataset)
    card_bytes = (dataset / "dataset_card.json").read_bytes()
    card = json.loads(card_bytes)
    bundle = load_model(model)
    if bundle["card"]["dataset"]["card_sha256"] != hashlib.sha256(card_bytes).hexdigest():
        raise ValueError(f"{model} was trained on a different build than {dataset}")
    if card["author_salt"] == "custom" and salt == DEFAULT_AUTHOR_SALT:
        raise ValueError("the dataset uses a custom author salt; pass the same salt to score new commits")

    git = Git(repo)
    sha = git.resolve_commit(commit)
    table = load_table(dataset)
    rows = table.index[table["sha"] == sha]
    in_dataset = len(rows) == 1

    found: dict[str, Any] = {}
    records: list[Mapping[str, Any]] = []

    def observe(record: Mapping[str, Any]) -> None:
        if record["commit"]["sha"] == sha:
            found["record"] = record
        if not in_dataset:
            records.append(record)

    index = SimilarityIndex.load(dataset, observe=observe)
    if in_dataset:
        row = table.loc[rows]
        record = found["record"]
        position = index.position[sha]
        landed = index.entries[position].landed
        split = index.entries[position].split
        neighbours = index.query_position(position, k)
    else:
        record = json.loads(record_to_json(mine_commit(git, sha, make_context(git, sha, salt=salt))))
        if record["provenance"]["taxonomy_sha"] != card["taxonomy_sha"]:
            raise ValueError("the taxonomy changed since the dataset was built; rebuild it before scoring")
        row, landed = _new_row(record, records, table)
        split = None
        neighbours = index.query_record(record, landed, k)

    spec = spec_from_dict(bundle["spec"])
    x = spec.transform(row)
    probability = float(predict(bundle, x)[0])
    reference = bundle["reference_scores"]
    category = categorize(record)
    return {
        "bundle_version": BUNDLE_VERSION,
        "commit": {
            "sha": sha,
            "subject": record["message"]["subject"],
            "committed_at": record["commit"]["committed_at"],
            "landed": utc(landed),
            "change_type": category.change_type,
            "change_types": list(category.change_types),
            "in_dataset": in_dataset,
            "split": split,
            "in_training_data": split == "train",
        },
        "document": llm_document(record),
        "risk": {
            "label": bundle["label"],
            "definition": LABEL_DEFINITIONS[bundle["label"]],
            "model": bundle["selected"],
            "probability": round(probability, 6),
            "percentile": round(100 * float(np.searchsorted(reference, probability, side="right")) / len(reference), 2),
            "base_rate": round(float(bundle["prevalence"]), 6),
            "reasons_model": "logistic",
            "reasons": logistic_reasons(bundle["models"]["logistic"], x[0], spec.names),
        },
        "similar": neighbours,
        "provenance": {
            "dataset_snapshot": card["snapshot"],
            "dataset_card_sha256": bundle["card"]["dataset"]["card_sha256"],
            "extractor_version": record["provenance"]["extractor_version"],
            "taxonomy_sha": card["taxonomy_sha"],
            "model_seed": bundle["card"]["seed"],
        },
    }


def _new_row(
    record: Mapping[str, Any], records: list[Mapping[str, Any]], table: pd.DataFrame
) -> tuple[pd.DataFrame, int]:
    """The feature row of a commit outside the dataset, landing right after the snapshot."""
    facts: list[CommitFacts] = []
    landed = 0
    for position, item in enumerate(records):
        landed = max(landed, item["commit"]["committed_epoch"])
        facts.append(facts_from_record(position, item, categorize(item), landed))
    landed = max(landed, record["commit"]["committed_epoch"])
    category = categorize(record)
    facts.append(facts_from_record(len(facts), record, category, landed))
    reverts, _ = link_reverts(facts[:-1])
    history = history_features(facts, {target: link.revert for target, link in reverts.items()})[-1]
    one_hot = [name for name in table.columns if name.startswith(ONE_HOT_PREFIXES)]
    row = feature_row(facts[-1], category, intrinsic_columns(record), history, one_hot, "scored")
    return pd.DataFrame([row]), landed
