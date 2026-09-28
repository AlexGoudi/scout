"""Deterministic JSON Lines shards: sorted keys, gzip with no timestamp or file name."""

from __future__ import annotations

import gzip
import io
import json
import os
from pathlib import Path
from typing import Any, Iterable, Iterator

SHARD_SIZE = 1000


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def gzip_bytes(data: bytes) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0, compresslevel=6) as handle:
        handle.write(data)
    return buffer.getvalue()


def write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp{os.getpid()}")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def write_text_atomic(path: Path, text: str) -> None:
    write_bytes_atomic(path, text.encode("utf-8"))


class ShardWriter:
    """Writes lines into ``part-NNNNN.jsonl[.gz]`` files in order, replacing any earlier parts."""

    def __init__(self, directory: Path, *, compress: bool = True, size: int = SHARD_SIZE) -> None:
        self.directory = Path(directory)
        self.compress = compress
        self.size = size
        self.names: list[str] = []
        self._batch: list[str] = []
        self.directory.mkdir(parents=True, exist_ok=True)
        for stale in self.directory.glob("part-*.jsonl*"):
            stale.unlink()

    def write(self, line: str) -> None:
        self._batch.append(line)
        if len(self._batch) == self.size:
            self._flush()

    def close(self) -> list[str]:
        if self._batch:
            self._flush()
        return self.names

    def _flush(self) -> None:
        name = f"part-{len(self.names):05d}.jsonl" + (".gz" if self.compress else "")
        data = "".join(line + "\n" for line in self._batch).encode("utf-8")
        write_bytes_atomic(self.directory / name, gzip_bytes(data) if self.compress else data)
        self.names.append(name)
        self._batch.clear()


def write_shards(directory: Path, lines: Iterable[str], *, compress: bool = True, size: int = SHARD_SIZE) -> list[str]:
    writer = ShardWriter(directory, compress=compress, size=size)
    for line in lines:
        writer.write(line)
    return writer.close()


def iter_lines(directory: Path) -> Iterator[str]:
    for path in sorted(Path(directory).glob("part-*.jsonl*")):
        if ".tmp" in path.suffix:
            continue
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                yield line.rstrip("\n")


def iter_json(directory: Path) -> Iterator[dict[str, Any]]:
    for line in iter_lines(directory):
        yield json.loads(line)
