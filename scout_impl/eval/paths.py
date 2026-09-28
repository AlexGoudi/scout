"""Calibration cache paths under eval_cache_root."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..repos.base import RepoAdapter
from ..source import LocalCheckout, RepoSource
from ._cache import seed_legacy_sha_subdirs
from .history import eval_cache_root


def revision_sha(source: RepoSource, ref: str = "HEAD") -> str:
    if isinstance(source, LocalCheckout):
        from ..gitcmd import GitRepo

        return GitRepo(source.root).rev_parse(ref)
    return source.rev_parse(ref)


def eval_calibration_dir(
    adapter: RepoAdapter,
    source: RepoSource | None = None,
    cache_root: Optional[Path] = None,
    revision: Optional[str] = None,
) -> Path:
    """Stable per-adapter calibration directory (no tip sha in the path)."""
    del source, revision  # tip is tracked in manifest cursors, not the directory name
    stable = eval_cache_root(cache_root) / "calibration" / adapter.name
    seed_legacy_sha_subdirs(stable)
    stable.mkdir(parents=True, exist_ok=True)
    return stable
