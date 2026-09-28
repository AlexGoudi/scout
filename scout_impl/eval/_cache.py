"""Incremental calibration cache: manifest and per-step fingerprints."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Sequence

MANIFEST_NAME = "manifest.json"
CACHE_SCHEMA_VERSION = 1
CODE_VERSION = "1"


def fingerprint(*parts: str) -> str:
    material = "\x00".join(parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_manifest(directory: Path) -> dict[str, Any]:
    path = directory / MANIFEST_NAME
    if not path.is_file():
        return {"schema_version": CACHE_SCHEMA_VERSION, "steps": {}}
    return json.loads(path.read_text(encoding="utf-8"))


class Step:
    """One cached mining step under a stable adapter calibration directory."""

    def __init__(self, directory: Path, name: str, step_fingerprint: str, refresh: bool = False) -> None:
        self.directory = Path(directory)
        self.name = name
        self.step_fingerprint = step_fingerprint
        self.refresh = refresh
        self._manifest = load_manifest(self.directory)

    def fresh(self) -> bool:
        if self.refresh:
            return False
        entry = (self._manifest.get("steps") or {}).get(self.name) or {}
        return entry.get("fingerprint") == self.step_fingerprint

    @property
    def cursor(self) -> Any:
        entry = (self._manifest.get("steps") or {}).get(self.name) or {}
        return entry.get("cursor")

    def commit(self, files: Sequence[str], cursor: Any) -> None:
        steps = dict(self._manifest.get("steps") or {})
        steps[self.name] = {
            "fingerprint": self.step_fingerprint,
            "cursor": cursor,
            "files": list(files),
        }
        self._manifest["schema_version"] = CACHE_SCHEMA_VERSION
        self._manifest["steps"] = steps
        atomic_write_text(
            self.directory / MANIFEST_NAME,
            json.dumps(self._manifest, indent=2, sort_keys=True) + "\n",
        )


def seed_legacy_sha_subdirs(stable: Path) -> None:
    """Copy the newest sha-keyed calibration subtree into a flat stable directory."""
    stable.mkdir(parents=True, exist_ok=True)
    if (stable / MANIFEST_NAME).is_file() or (stable / "git-overview.json").is_file():
        return
    legacy_dirs = [
        path
        for path in stable.iterdir()
        if path.is_dir() and path.name not in {"online"} and (path / "git-overview.json").is_file()
    ]
    if not legacy_dirs:
        return
    source = max(legacy_dirs, key=lambda item: item.stat().st_mtime)
    for item in source.iterdir():
        if item.is_file():
            dest = stable / item.name
            if not dest.exists():
                shutil.copy2(item, dest)
