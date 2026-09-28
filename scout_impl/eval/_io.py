"""JSON dump/load and progress helpers (mining / eval)."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def dump(out_dir: str, name: str, obj, indent=2) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, name)
    with open(path, "w") as f:
        if indent is None:
            if isinstance(obj, list):
                for row in obj:
                    f.write(json.dumps(row, default=str) + "\n")
            else:
                json.dump(obj, f, default=str)
                f.write("\n")
        else:
            json.dump(obj, f, indent=indent, default=str)
            f.write("\n")
    logger.debug("wrote %s (%d bytes)", name, os.path.getsize(path))
    return path


def cache_path(out_dir: str, name: str) -> str:
    return os.path.join(out_dir, name)


def cache_exists(out_dir: str, name: str) -> bool:
    return os.path.isfile(cache_path(out_dir, name))


def try_load(out_dir: str, name: str):
    path = cache_path(out_dir, name)
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        return json.load(f)


def load(out_dir: str, name: str):
    with open(cache_path(out_dir, name)) as f:
        return json.load(f)


def progress_line(done: int, total: int, label: str, width: int = 28, unit: str = "") -> str:
    done = max(int(done), 0)
    total = max(int(total), 0)
    suffix = f" {unit}" if unit else ""
    if total <= 0:
        return f"{label} {done}{suffix}".rstrip()
    frac = min(done / total, 1.0)
    filled = min(int(round(width * frac)), width)
    bar = "#" * filled + "-" * (width - filled)
    return f"{label} [{bar}] {done}/{total}{suffix}"


def progress(
    done: int,
    total: int,
    label: str,
    width: int = 28,
    every: int = 25,
    unit: str = "",
) -> None:
    line = progress_line(done, total, label, width, unit)
    if total and done >= total:
        logger.info("%s", line)
        return
    if not logger.isEnabledFor(logging.DEBUG):
        return
    if total and done not in {1, total} and every > 1 and done % every != 0:
        return
    logger.debug("%s", line)
