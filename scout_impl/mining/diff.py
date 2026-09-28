"""Parse a stream of ``--unified=0`` patches into per-file hunks and bounded patch text.

Hunk ranges, hunk scopes and line-kind counts are kept for every file; patch lines are kept
only up to a per-file limit, so a vendor SDK drop costs memory proportional to the limit
rather than to its size.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Iterator

from .gitio import PATCH_MARKER
from .lines import comment_markers, indentation_matters, line_kind, squeeze

HUNK_RE = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
LINE_KINDS = ("blank", "whitespace_only", "comment")
ADDED, DELETED = 0, 1
SCOPE_MAX_BYTES = 500
SCOPES_KEPT = 64
PAIRING_MAX_LINES = 5_000
C_ESCAPES = {
    ord("a"): 7, ord("b"): 8, ord("t"): 9, ord("n"): 10, ord("v"): 11,
    ord("f"): 12, ord("r"): 13, ord('"'): 34, ord("\\"): 92,
}

Key = tuple["str | None", "str | None"]


@dataclass(frozen=True)
class PatchLimits:
    max_lines_per_file: int = 400
    max_line_bytes: int = 2_000


@dataclass
class FilePatch:
    old_path: str | None = None
    new_path: str | None = None
    hunks: list[tuple[int, int, int, int]] = field(default_factory=list)
    lines: list[bytes] = field(default_factory=list)
    line_count: int = 0
    omitted_long_lines: int = 0
    binary: bool = False
    scopes: list[str] = field(default_factory=list)
    line_kinds: dict[str, list[int]] = field(default_factory=lambda: {kind: [0, 0] for kind in LINE_KINDS})

    @property
    def key(self) -> Key:
        return (self.old_path, self.new_path)

    @property
    def truncated(self) -> bool:
        return self.line_count > len(self.lines) or self.omitted_long_lines > 0


class _FileState:
    def __init__(self, header: bytes) -> None:
        self.header = header
        self.patch = FilePatch()
        self.minus: str | None = None
        self.plus: str | None = None
        self.rename_from: str | None = None
        self.rename_to: str | None = None
        self.added = False
        self.deleted = False
        self.in_hunk = False
        self.markers: tuple[bytes, ...] | None = None
        self.keep_indent = False
        self.pending: dict[bytes, list[str]] = {}
        self.pending_lines = 0

    def start_hunk(self, rest: bytes) -> None:
        self.flush()
        self.in_hunk = True
        if self.markers is None:
            path = self.rename_to or self.plus or self.rename_from or self.minus
            path = path or _symmetric_header_path(self.header)
            self.markers = comment_markers(path)
            self.keep_indent = indentation_matters(path)
        scope = rest[:SCOPE_MAX_BYTES].strip().decode("utf-8", "replace")
        if scope and scope not in self.patch.scopes and len(self.patch.scopes) < SCOPES_KEPT:
            self.patch.scopes.append(scope)

    def count(self, line: bytes) -> None:
        """Tally a changed line; deletions wait until the hunk ends to pair with re-indented additions."""
        content = line[1:]
        kind = line_kind(content, self.markers or ())
        counts = self.patch.line_kinds
        if kind == "blank":
            counts["blank"][ADDED if line[:1] == b"+" else DELETED] += 1
        elif line[:1] == b"-":
            if self.pending_lines < PAIRING_MAX_LINES:
                self.pending.setdefault(squeeze(content, self.keep_indent), []).append(kind)
                self.pending_lines += 1
            elif kind == "comment":
                counts["comment"][DELETED] += 1
        else:
            waiting = self.pending.get(squeeze(content, self.keep_indent)) if self.pending else None
            if waiting:
                waiting.pop()
                counts["whitespace_only"][ADDED] += 1
                counts["whitespace_only"][DELETED] += 1
            elif kind == "comment":
                counts["comment"][ADDED] += 1

    def flush(self) -> None:
        for kinds in self.pending.values():
            self.patch.line_kinds["comment"][DELETED] += kinds.count("comment")
        self.pending = {}
        self.pending_lines = 0

    def finish(self) -> FilePatch:
        self.flush()
        old = self.rename_from or self.minus
        new = self.rename_to or self.plus
        if old is None and new is None:
            old = new = _symmetric_header_path(self.header)
        if self.added:
            old = None
        if self.deleted:
            new = None
        self.patch.old_path, self.patch.new_path = old, new
        return self.patch


def iter_commit_patches(
    lines: Iterable[bytes], limits: PatchLimits = PatchLimits()
) -> Iterator[tuple[str, dict[Key, list[FilePatch]]]]:
    """Yield ``(sha, patches by (old_path, new_path))`` per commit marker in the stream."""
    sha: str | None = None
    files: dict[Key, list[FilePatch]] = {}
    state: _FileState | None = None

    def close_file() -> None:
        nonlocal state
        if state is not None:
            patch = state.finish()
            files.setdefault(patch.key, []).append(patch)
        state = None

    for line in lines:
        if line.startswith(PATCH_MARKER):
            close_file()
            if sha is not None:
                yield sha, files
            sha = line[len(PATCH_MARKER):].decode("ascii", "replace").strip()
            files = {}
            continue
        if line.startswith(b"diff --git "):
            close_file()
            state = _FileState(line)
            continue
        if state is None:
            continue
        hunk = HUNK_RE.match(line)
        if hunk is not None:
            state.start_hunk(line[hunk.end():])
            old_start, old_count, new_start, new_count = hunk.groups()
            state.patch.hunks.append(
                (int(old_start), int(old_count or b"1"), int(new_start), int(new_count or b"1"))
            )
            _keep(state.patch, line, limits)
            continue
        if state.in_hunk:
            if line[:1] in (b"+", b"-"):
                state.count(line)
                _keep(state.patch, line, limits)
            elif line[:1] == b"\\":
                _keep(state.patch, line, limits)
            continue
        _read_header_line(state, line)
    close_file()
    if sha is not None:
        yield sha, files


def merge_patches(patches: list[FilePatch]) -> FilePatch:
    """One section per key is the norm; a type change arrives as a delete plus an add."""
    if len(patches) == 1:
        return patches[0]
    merged = FilePatch(old_path=patches[0].old_path, new_path=patches[0].new_path)
    for patch in patches:
        merged.hunks.extend(patch.hunks)
        merged.lines.extend(patch.lines)
        merged.line_count += patch.line_count
        merged.omitted_long_lines += patch.omitted_long_lines
        merged.binary = merged.binary or patch.binary
        merged.scopes.extend(scope for scope in patch.scopes if scope not in merged.scopes)
        for kind, (added, deleted) in patch.line_kinds.items():
            merged.line_kinds[kind][ADDED] += added
            merged.line_kinds[kind][DELETED] += deleted
    return merged


def _keep(patch: FilePatch, line: bytes, limits: PatchLimits) -> None:
    patch.line_count += 1
    if len(patch.lines) >= limits.max_lines_per_file:
        return
    if len(line) > limits.max_line_bytes:
        patch.omitted_long_lines += 1
        line = line[:1] + b"<line omitted: %d bytes>" % len(line)
    patch.lines.append(line)


def _read_header_line(state: _FileState, line: bytes) -> None:
    if line.startswith(b"--- "):
        state.minus = _side_path(line[4:], b"a/")
    elif line.startswith(b"+++ "):
        state.plus = _side_path(line[4:], b"b/")
    elif line.startswith((b"rename from ", b"copy from ")):
        state.rename_from = _decode(_unquote(line.split(b" ", 2)[2]))
    elif line.startswith((b"rename to ", b"copy to ")):
        state.rename_to = _decode(_unquote(line.split(b" ", 2)[2]))
    elif line.startswith(b"new file mode "):
        state.added = True
    elif line.startswith(b"deleted file mode "):
        state.deleted = True
    elif line.startswith(b"Binary files ") or line == b"GIT binary patch":
        state.patch.binary = True


def _side_path(value: bytes, prefix: bytes) -> str | None:
    if value.endswith(b"\t"):
        value = value[:-1]
    if value == b"/dev/null":
        return None
    value = _unquote(value)
    return _decode(value[len(prefix):] if value.startswith(prefix) else value)


def _symmetric_header_path(header: bytes) -> str | None:
    """Recover ``P`` from ``diff --git a/P b/P``, the only form without ---/+++ or rename lines."""
    rest = header[len(b"diff --git "):]
    if rest.startswith(b'"'):
        quoted = re.findall(rb'"(?:[^"\\]|\\.)*"', rest)
        return _decode(_unquote(quoted[-1])[2:]) if quoted else None
    length = len(rest) - len(b"a/ b/")
    if length <= 0 or length % 2:
        return None
    candidate = rest[2:2 + length // 2]
    return _decode(candidate) if rest == b"a/" + candidate + b" b/" + candidate else None


def _unquote(value: bytes) -> bytes:
    """Undo Git's C-style quoting of unusual paths."""
    if len(value) < 2 or not (value.startswith(b'"') and value.endswith(b'"')):
        return value
    body = value[1:-1]
    output = bytearray()
    index = 0
    while index < len(body):
        byte = body[index]
        if byte != 0x5C or index + 1 >= len(body):
            output.append(byte)
            index += 1
            continue
        octal = body[index + 1:index + 4]
        if len(octal) == 3 and all(0x30 <= item <= 0x37 for item in octal):
            output.append(int(octal, 8))
            index += 4
        else:
            output.append(C_ESCAPES.get(body[index + 1], body[index + 1]))
            index += 2
    return bytes(output)


def _decode(value: bytes) -> str:
    return value.decode("utf-8", "surrogateescape")
