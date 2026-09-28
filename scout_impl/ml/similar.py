"""Similar earlier commits: 0.6 x TF-IDF cosine + 0.4 x Jaccard over files, areas and entities.

Only commits that landed before the query are returned, and a neighbour's outcome counts
only if its revert or fix landed before the query too. IDF is fitted on the commits that
landed before the validation split starts, so evaluating on test borrows no later text.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import pandas as pd
from scipy import sparse

from ..dataset.shards import iter_json
from ..mining.record import utc

TEXT_WEIGHT = 0.6
SET_WEIGHT = 0.4
TEXT_BODY_MAX = 3_000
QUERY_CHUNK = 256
PATH_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]+")
OUTCOMES = ("bug_introducing", "reverted")


@dataclass(frozen=True)
class Entry:
    sha: str
    index: int
    landed: int
    subject: str
    change_type: str
    split: str
    is_merge: bool
    labels: Mapping[str, Any]


def commit_text(record: Mapping[str, Any]) -> str:
    message = record["message"]
    parts = [message["subject"]]
    sections = [text for text in message["sections"].values() if text]
    parts += sections if sections else [message["body"][:TEXT_BODY_MAX]]
    for item in record["files"]:
        for path in {item["old_path"], item["new_path"]} - {None}:
            parts.append(" ".join(PATH_TOKEN_RE.findall(path)))
    return "\n".join(parts)


def commit_items(record: Mapping[str, Any]) -> set[str]:
    items = {f"file:{path}" for item in record["files"] for path in (item["old_path"], item["new_path"]) if path}
    areas = record["areas"]
    items.update(f"component:{item['id']}" for item in areas["components"])
    items.update(f"feature:{item['id']}" for item in areas["features"])
    items.update(f"entity:{item['id']}" for item in areas["entities"])
    return items


def known_outcomes(labels: Mapping[str, Any], at: int) -> dict[str, bool | None]:
    """A neighbour's outcomes as they were known at time ``at``."""
    reverted = labels.get("reverted")
    bug = labels.get("bug_introducing")
    return {
        "reverted": None if reverted is None else bool(reverted and labels["reverted_landed"] < at),
        "bug_introducing": None if bug is None else bool(bug and labels["fixed_landed"] < at),
    }


class SimilarityIndex:
    def __init__(self, entries: Sequence[Entry], texts: Sequence[str], items: Sequence[set[str]], fit_until: int):
        from sklearn.feature_extraction.text import TfidfVectorizer

        self.entries = list(entries)
        self.position = {entry.sha: position for position, entry in enumerate(self.entries)}
        self.landed = np.array([entry.landed for entry in self.entries], dtype=np.int64)
        fit_texts = [text for text, entry in zip(texts, self.entries) if entry.landed < fit_until] or list(texts)
        self.vectorizer = TfidfVectorizer(
            sublinear_tf=True, min_df=2, max_features=100_000, token_pattern=r"(?u)\b[A-Za-z][A-Za-z0-9_]+\b"
        ).fit(fit_texts)
        self.text = self.vectorizer.transform(texts).tocsr()
        vocabulary = sorted(set().union(*items)) if items else []
        self.item_columns = {item: column for column, item in enumerate(vocabulary)}
        self.sets = self._incidence(items)
        self.set_sizes = np.asarray(self.sets.sum(axis=1)).ravel()
        self.candidate = np.array([not entry.is_merge and entry.split != "holdout" for entry in self.entries])

    @classmethod
    def load(
        cls, dataset: str | Path, observe: Callable[[Mapping[str, Any]], None] | None = None
    ) -> "SimilarityIndex":
        """Index a dataset directory; ``observe`` sees every record as it is read."""
        dataset = Path(dataset)
        card = json.loads((dataset / "dataset_card.json").read_text())
        annotations = list(iter_json(dataset / "annotations"))
        entries, texts, items = [], [], []
        for record, annotation in zip(iter_json(dataset / "records"), annotations):
            if record["commit"]["sha"] != annotation["sha"]:
                raise ValueError(f"records and annotations disagree at {annotation['sha']}")
            if observe is not None:
                observe(record)
            entries.append(
                Entry(
                    sha=annotation["sha"],
                    index=annotation["index"],
                    landed=annotation["landed"],
                    subject=record["message"]["subject"],
                    change_type=annotation["category"]["change_type"],
                    split=annotation["split"],
                    is_merge=record["commit"]["is_merge"],
                    labels=annotation["labels"],
                )
            )
            texts.append(commit_text(record))
            items.append(commit_items(record))
        fit_until = _epoch(card["splits"]["boundaries"]["validation_start"])
        return cls(entries, texts, items, fit_until)

    def resolve(self, prefix: str) -> int:
        prefix = prefix.strip().lower()
        matches = [position for sha, position in self.position.items() if sha.startswith(prefix)] if prefix else []
        if len(matches) != 1:
            raise ValueError(f"{prefix!r} matches {len(matches)} commits in the dataset")
        return matches[0]

    def query_sha(self, prefix: str, k: int = 10) -> dict[str, Any]:
        position = self.resolve(prefix)
        entry = self.entries[position]
        return {
            "query": {"sha": entry.sha, "subject": entry.subject, "landed": utc(entry.landed),
                      "change_type": entry.change_type},
            "neighbours": self.query_position(position, k),
        }

    def query_position(self, position: int, k: int = 10) -> list[dict[str, Any]]:
        entry = self.entries[position]
        older = self.candidate & (np.arange(len(self.entries)) < position)
        return self._neighbours(self.text[position], self.sets[position], self.set_sizes[position], older,
                                entry.landed, k)

    def query_record(self, record: Mapping[str, Any], landed: int, k: int = 10) -> list[dict[str, Any]]:
        """Neighbours of a commit outside the dataset among commits that landed before ``landed``."""
        text = self.vectorizer.transform([commit_text(record)]).tocsr()
        sets = self._incidence([commit_items(record)])
        older = self.candidate & (self.landed < landed)
        return self._neighbours(text, sets, float(sets.sum()), older, landed, k)

    def evaluate_lift(self, query_labels: Mapping[str, bool | None], label: str, k: int = 10) -> dict[str, Any]:
        """Neighbour positive rate for positive queries divided by the rate for negative queries."""
        if label not in OUTCOMES:
            raise ValueError(f"label must be one of {OUTCOMES}")
        rates: dict[bool, list[float]] = {True: [], False: []}
        queries = [(self.position[sha], value) for sha, value in query_labels.items() if value is not None]
        for start in range(0, len(queries), QUERY_CHUNK):
            chunk = queries[start:start + QUERY_CHUNK]
            rows = [position for position, _ in chunk]
            combined = self._scores(self.text[rows], self.sets[rows], self.set_sizes[rows])[0]
            for row, (position, value) in enumerate(chunk):
                older = self.candidate & (np.arange(len(self.entries)) < position)
                top = _top(combined[row], older, k)
                known = [known_outcomes(self.entries[j].labels, self.entries[position].landed)[label] for j in top]
                known = [outcome for outcome in known if outcome is not None]
                if known:
                    rates[bool(value)].append(sum(known) / len(known))
        positive = float(np.mean(rates[True])) if rates[True] else None
        negative = float(np.mean(rates[False])) if rates[False] else None
        return {
            "label": label,
            "k": k,
            "queries": {"positive": len(rates[True]), "negative": len(rates[False])},
            "neighbour_positive_rate": {"positive_queries": _round(positive), "negative_queries": _round(negative)},
            "lift": _round(positive / negative) if positive is not None and negative else None,
        }

    def _neighbours(self, text, sets, size, allowed: np.ndarray, at: int, k: int) -> list[dict[str, Any]]:
        combined, cosine, jaccard = self._scores(text, sets, np.atleast_1d(size))
        top = _top(combined[0], allowed, k)
        query_items = set(sets.indices)
        names = {column: item for item, column in self.item_columns.items()}
        neighbours = []
        for j in top:
            entry = self.entries[j]
            shared = sorted(names[column] for column in query_items & set(self.sets[j].indices))
            neighbours.append(
                {
                    "sha": entry.sha,
                    "subject": entry.subject,
                    "landed": utc(entry.landed),
                    "change_type": entry.change_type,
                    "score": _round(combined[0, j]),
                    "text_similarity": _round(cosine[0, j]),
                    "jaccard": _round(jaccard[0, j]),
                    "shared": shared[:10],
                    "outcomes_known_at_query": known_outcomes(entry.labels, at),
                }
            )
        return neighbours

    def _scores(self, text, sets, sizes) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        cosine = (text @ self.text.T).toarray()
        intersection = (sets @ self.sets.T).toarray()
        union = np.asarray(sizes, dtype=float)[:, None] + self.set_sizes[None, :] - intersection
        jaccard = np.divide(intersection, union, out=np.zeros_like(intersection, dtype=float), where=union > 0)
        return TEXT_WEIGHT * cosine + SET_WEIGHT * jaccard, cosine, jaccard

    def _incidence(self, items: Sequence[set[str]]) -> sparse.csr_matrix:
        rows, columns = [], []
        for row, values in enumerate(items):
            for item in values:
                column = self.item_columns.get(item)
                if column is not None:
                    rows.append(row)
                    columns.append(column)
        data = np.ones(len(rows), dtype=np.float64)
        return sparse.csr_matrix((data, (rows, columns)), shape=(len(items), len(self.item_columns)))


def evaluate_similarity(dataset: str | Path, k: int = 10, split: str = "test") -> dict[str, Any]:
    """Neighbour lift for each outcome, over the split's non-merge commits and their split-aware labels."""
    from .matrix import load_table

    index = SimilarityIndex.load(dataset)
    table = load_table(dataset)
    rows = table[(table["split"] == split) & ~table["is_merge"].astype(bool)]
    report = {}
    for label in OUTCOMES:
        values = rows[f"label_{label}"]
        queries = {sha: None if pd.isna(value) else bool(value) for sha, value in zip(rows["sha"], values)}
        report[label] = index.evaluate_lift(queries, label, k)
    return {"split": split, "k": k, "weights": {"text": TEXT_WEIGHT, "sets": SET_WEIGHT}, "labels": report}


def _top(scores: np.ndarray, allowed: np.ndarray, k: int) -> list[int]:
    candidates = np.nonzero(allowed)[0]
    if len(candidates) == 0 or k <= 0:
        return []
    values = scores[candidates]
    order = np.lexsort((-candidates, -values))[:k]
    return [int(candidates[i]) for i in order]


def _epoch(stamp: str) -> int:
    from datetime import datetime, timezone

    return int(datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 6)
