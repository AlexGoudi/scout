"""Parser turning `git diff` output into `FileDiff` objects.

Kept free of subprocess calls so the structural behaviour can be tested against fixed
diff text rather than against whatever the repo history happens to contain.
"""

import re
from typing import List, Optional, Tuple

from .models import (
    CHANGE_ADDED,
    CHANGE_COPIED,
    CHANGE_DELETED,
    CHANGE_MODIFIED,
    CHANGE_RENAMED,
    DiffHunk,
    DiffLine,
    FileDiff,
    LINE_ADDED,
    LINE_CONTEXT,
    LINE_REMOVED,
)
from .repos import RepoAdapter

_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
_SIMILARITY_RE = re.compile(r"^(?:similarity|dissimilarity) index (\d+)%$")
_DEV_NULL = "/dev/null"


def parse_diff(text: str, adapter: RepoAdapter) -> List[FileDiff]:
    """Parse a multi-file unified diff produced with `--find-renames`.

    `adapter` supplies the classification rules, so the same diff text classifies
    differently depending on which repository it came from.
    """
    file_diffs: List[FileDiff] = []
    for block in _split_file_blocks(text):
        parsed = _parse_file_block(block, adapter)
        if parsed is not None:
            file_diffs.append(parsed)
    return file_diffs


def _split_file_blocks(text: str) -> List[List[str]]:
    blocks: List[List[str]] = []
    current: Optional[List[str]] = None
    for line in (text or "").splitlines():
        if line.startswith("diff --git "):
            current = [line]
            blocks.append(current)
        elif current is not None:
            current.append(line)
    return blocks


def _parse_file_block(block: List[str], adapter: RepoAdapter) -> Optional[FileDiff]:
    header_old, header_new = _split_diff_git_paths(block[0])
    old_path = header_old
    new_path = header_new
    change_type = CHANGE_MODIFIED
    is_binary = False
    similarity: Optional[int] = None
    hunk_lines: List[str] = []

    index = 1
    while index < len(block):
        line = block[index]
        if line.startswith("@@"):
            hunk_lines = block[index:]
            break
        if line.startswith("new file mode "):
            change_type = CHANGE_ADDED
            old_path = None
        elif line.startswith("deleted file mode "):
            change_type = CHANGE_DELETED
            new_path = None
        elif line.startswith("rename from "):
            change_type = CHANGE_RENAMED
            old_path = _unquote_path(line[len("rename from "):])
        elif line.startswith("rename to "):
            new_path = _unquote_path(line[len("rename to "):])
        elif line.startswith("copy from "):
            change_type = CHANGE_COPIED
            old_path = _unquote_path(line[len("copy from "):])
        elif line.startswith("copy to "):
            new_path = _unquote_path(line[len("copy to "):])
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            is_binary = True
        elif line.startswith("--- "):
            parsed = _strip_side_prefix(line[4:])
            if parsed is None:
                change_type = CHANGE_ADDED
                old_path = None
            elif change_type not in (CHANGE_RENAMED, CHANGE_COPIED):
                old_path = parsed
        elif line.startswith("+++ "):
            parsed = _strip_side_prefix(line[4:])
            if parsed is None:
                change_type = CHANGE_DELETED
                new_path = None
            elif change_type not in (CHANGE_RENAMED, CHANGE_COPIED):
                new_path = parsed
        else:
            match = _SIMILARITY_RE.match(line)
            if match:
                similarity = int(match.group(1))
        index += 1

    path = new_path or old_path
    if not path:
        return None

    return FileDiff(
        path=path,
        change_type=change_type,
        path_class=adapter.classify(path),
        old_path=old_path if old_path and old_path != path else None,
        is_binary=is_binary,
        similarity=similarity,
        hunks=_parse_hunks(hunk_lines),
    )


def _parse_hunks(lines: List[str]) -> List[DiffHunk]:
    hunks: List[DiffHunk] = []
    old_lineno = 0
    new_lineno = 0
    current: Optional[DiffHunk] = None

    for line in lines:
        header = _HUNK_HEADER_RE.match(line)
        if header:
            old_start = int(header.group(1))
            old_count = int(header.group(2)) if header.group(2) is not None else 1
            new_start = int(header.group(3))
            new_count = int(header.group(4)) if header.group(4) is not None else 1
            current = DiffHunk(
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
                section=header.group(5).strip(),
                lines=[],
            )
            hunks.append(current)
            old_lineno = old_start
            new_lineno = new_start
            continue

        if current is None:
            continue

        # "\ No newline at end of file" annotates the previous line rather than adding one.
        if line.startswith("\\"):
            continue

        kind = line[:1] or LINE_CONTEXT
        content = line[1:]
        if kind == LINE_ADDED:
            current.lines.append(DiffLine(kind=kind, content=content, new_lineno=new_lineno))
            new_lineno += 1
        elif kind == LINE_REMOVED:
            current.lines.append(DiffLine(kind=kind, content=content, old_lineno=old_lineno))
            old_lineno += 1
        elif kind == LINE_CONTEXT:
            current.lines.append(
                DiffLine(kind=kind, content=content, old_lineno=old_lineno, new_lineno=new_lineno)
            )
            old_lineno += 1
            new_lineno += 1

    return hunks


def _split_diff_git_paths(line: str) -> Tuple[Optional[str], Optional[str]]:
    """Best-effort split of `diff --git a/OLD b/NEW`, refined later by the `---`/`+++` lines."""
    remainder = line[len("diff --git "):]
    if remainder.startswith('"'):
        return None, None

    midpoint = remainder.find(" b/")
    if midpoint == -1:
        return None, None

    old_path = _strip_side_prefix(remainder[:midpoint])
    new_path = _strip_side_prefix(remainder[midpoint + 1:])
    return old_path, new_path


def _strip_side_prefix(raw: str) -> Optional[str]:
    """Strip the `a/` or `b/` side prefix; `/dev/null` means the side does not exist."""
    # git appends a tab before trailing whitespace or a timestamp in some configurations.
    value = _unquote_path(raw.split("\t")[0].strip())
    if not value or value == _DEV_NULL:
        return None
    if value.startswith("a/") or value.startswith("b/"):
        return value[2:]
    return value


def _unquote_path(raw: str) -> str:
    """Undo git's C-style quoting, which core.quotepath=false limits to quotes and controls."""
    value = raw.strip()
    if len(value) < 2 or not value.startswith('"') or not value.endswith('"'):
        return value

    inner = value[1:-1]
    return (
        inner.replace('\\"', '"')
        .replace("\\t", "\t")
        .replace("\\n", "\n")
        .replace("\\\\", "\\")
    )
