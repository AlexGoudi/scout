"""The model matrix: rows and columns of ``features.parquet`` a model may use, transformed.

Counts go through ``log1p``; rates, shares and indicators stay as they are; a missing value
becomes 0 plus a ``__missing`` indicator; the primary change type is one-hot encoded. Merge,
gap and holdout rows never reach a model.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from ..dataset.categories import PRECEDENCE

SPLITS = ("train", "validation", "test")
ID_COLUMNS = ("sha", "index", "landed", "committed_at", "split", "change_type", "is_merge")
LABEL_PREFIX = "label_"
AS_IS_MARKERS = ("rate", "share", "normalized", "__")
# Stay in the feature table for analysis, but never reach a model: who wrote a change is not a
# property of the change.
IDENTITY_PREFIXES = ("author_", "committer_", "file_prior_authors")


@dataclass(frozen=True)
class ColumnSpec:
    """How to turn feature-table columns into model inputs; saved with every model."""

    numeric: tuple[str, ...]
    log_columns: tuple[str, ...]
    missing_columns: tuple[str, ...]

    @property
    def names(self) -> list[str]:
        return (
            list(self.numeric)
            + [f"{name}__missing" for name in self.missing_columns]
            + [f"change_type={kind}" for kind in PRECEDENCE]
        )

    def transform(self, frame: pd.DataFrame) -> np.ndarray:
        columns = []
        logged = set(self.log_columns)
        for name in self.numeric:
            values = pd.to_numeric(frame[name], errors="coerce").astype(float).to_numpy()
            values = np.nan_to_num(values, nan=0.0)
            columns.append(np.log1p(np.clip(values, 0, None)) if name in logged else values)
        for name in self.missing_columns:
            columns.append(frame[name].isna().to_numpy(dtype=float))
        kinds = frame["change_type"].astype(str).to_numpy()
        for kind in PRECEDENCE:
            columns.append((kinds == kind).astype(float))
        return np.column_stack(columns) if columns else np.empty((len(frame), 0))

    def to_dict(self) -> dict[str, list[str]]:
        return {"numeric": list(self.numeric), "log": list(self.log_columns), "missing": list(self.missing_columns)}


@dataclass(frozen=True)
class Matrix:
    frame: pd.DataFrame
    x: np.ndarray
    y: np.ndarray
    spec: ColumnSpec

    def mask(self, split: str) -> np.ndarray:
        return (self.frame["split"] == split).to_numpy()

    @property
    def effort(self) -> np.ndarray:
        """Changed lines to inspect, at least one per commit."""
        return np.maximum(self.frame["churn"].to_numpy(dtype=float), 1.0)


def load_table(dataset: str | Path) -> pd.DataFrame:
    import pyarrow.parquet as pq

    path = Path(dataset) / "features.parquet"
    if not path.is_file():
        raise FileNotFoundError(f"{path} does not exist; run 'dataset build' first")
    return pq.read_table(path).to_pandas()


def column_spec(frame: pd.DataFrame) -> ColumnSpec:
    numeric = []
    for name in frame.columns:
        if name in ID_COLUMNS or name.startswith(LABEL_PREFIX) or name.startswith(IDENTITY_PREFIXES):
            continue
        if pd.api.types.is_bool_dtype(frame[name]) or pd.api.types.is_numeric_dtype(frame[name]):
            numeric.append(name)
        elif frame[name].dtype == object and frame[name].dropna().map(lambda value: isinstance(value, bool)).all():
            numeric.append(name)
    log_columns = [
        name
        for name in numeric
        if not pd.api.types.is_bool_dtype(frame[name])
        and not any(marker in name for marker in AS_IS_MARKERS)
        and not name.startswith(("is_", "has_", "touches_"))
    ]
    missing = [name for name in numeric if frame[name].isna().any()]
    return ColumnSpec(tuple(numeric), tuple(log_columns), tuple(missing))


def build_matrix(frame: pd.DataFrame, label: str, spec: ColumnSpec | None = None) -> Matrix:
    """Rows of train, validation and test that are not merges and have a known ``label``."""
    column = f"{LABEL_PREFIX}{label}"
    if column not in frame.columns:
        raise ValueError(f"{column} is not in the feature table")
    keep = frame["split"].isin(SPLITS) & ~frame["is_merge"].astype(bool) & frame[column].notna()
    rows = frame.loc[keep].reset_index(drop=True)
    spec = spec or column_spec(frame)
    return Matrix(frame=rows, x=spec.transform(rows), y=rows[column].astype(bool).to_numpy(dtype=float), spec=spec)


def spec_from_dict(data: Mapping[str, Sequence[Any]]) -> ColumnSpec:
    return ColumnSpec(tuple(data["numeric"]), tuple(data["log"]), tuple(data["missing"]))
