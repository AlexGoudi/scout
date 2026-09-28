"""Read-only access to a local Git repository.

Every call passes its arguments as an array with no shell, pins the configuration the
parsers depend on, and reads objects only: nothing is checked out, nothing from the analyzed
revision is executed, and no command that writes to the repository is ever issued.
"""

from __future__ import annotations

import codecs
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence
from urllib.parse import urlsplit, urlunsplit

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
ZERO_SHA = "0" * 40
GITLINK_MODE = "160000"
SHA_RE = re.compile(r"[0-9a-f]{40}")

DIFF_DRIVERS = Path(__file__).resolve().with_name("diff-drivers.gitattributes")
YANG_SCOPE_RE = (
    r"^[[:space:]]*((module|submodule|container|list|grouping|leaf|leaf-list|augment|rpc|notification"
    r"|typedef|choice|case|identity)[[:space:]].*)$"
)

# Repository, user and system configuration can change path quoting, prefixes, rename
# pairing, hunk boundaries and hunk headers, and the parsers depend on every one of them.
PINNED_CONFIG = (
    "-c", "core.quotePath=false",
    "-c", f"core.attributesFile={DIFF_DRIVERS}",
    "-c", f"diff.yang.xfuncname={YANG_SCOPE_RE}",
    "-c", "i18n.logOutputEncoding=UTF-8",
    "-c", "color.ui=false",
    "-c", "diff.algorithm=myers",
    "-c", "diff.indentHeuristic=true",
    "-c", "diff.interHunkContext=0",
    "-c", "diff.renameLimit=1000",
    "-c", "diff.noprefix=false",
    "-c", "diff.mnemonicPrefix=false",
    "-c", "diff.relative=false",
    "-c", "diff.suppressBlankEmpty=false",
    "-c", "log.showRoot=true",
    "-c", "log.showSignature=false",
)
DIFF_ARGS = (
    "--no-ext-diff",
    "--no-textconv",
    "--no-relative",
    "--no-color",
    "--find-renames=50%",
    "--diff-merges=first-parent",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)
# Variables that would silently point Git at another repository or rewrite its history view.
DROPPED_ENVIRONMENT = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_NAMESPACE",
    "GIT_COMMON_DIR",
    "GIT_CONFIG_PARAMETERS",
    "GIT_EXTERNAL_DIFF",
    "GIT_DIFF_OPTS",
)
PATCH_MARKER = b"\x00\x01"


class GitError(RuntimeError):
    """Raised when repository data cannot be read completely and safely."""


@dataclass(frozen=True)
class CommitObject:
    """One commit object as stored. Holds author identity, so it is never serialized."""

    sha: str
    tree: str
    parents: tuple[str, ...]
    author_name: str
    author_email: str
    authored_epoch: int
    committer_name: str
    committed_epoch: int
    message: str


@dataclass(frozen=True)
class RawChange:
    """One changed path against the first parent, from ``--raw`` joined with ``--numstat``."""

    status: str
    old_path: str | None
    new_path: str | None
    old_mode: str | None
    new_mode: str | None
    old_blob: str | None
    new_blob: str | None
    similarity: int | None
    additions: int | None
    deletions: int | None
    binary: bool

    @property
    def key(self) -> tuple[str | None, str | None]:
        return (self.old_path, self.new_path)

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""

    @property
    def is_gitlink(self) -> bool:
        return GITLINK_MODE in (self.old_mode, self.new_mode)


class Git:
    """Binary-safe, read-only access to one local repository."""

    def __init__(self, repository: str | Path, *, timeout_seconds: int = 900) -> None:
        self.repository = Path(repository).expanduser().resolve()
        self.timeout_seconds = timeout_seconds
        if not self.repository.is_dir():
            raise GitError(f"repository does not exist: {self.repository}")
        if self.run(("rev-parse", "--git-dir"), check=False).returncode != 0:
            raise GitError(f"not a Git repository: {self.repository}")

    # -- plumbing ---------------------------------------------------------------------------

    def command(self, arguments: Sequence[str]) -> list[str]:
        return ["git", "-C", str(self.repository), *PINNED_CONFIG, *arguments]

    def environment(self) -> dict[str, str]:
        environment = {key: value for key, value in os.environ.items() if key not in DROPPED_ENVIRONMENT}
        environment.update(
            {
                "LC_ALL": "C",
                "LANG": "C",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ATTR_NOSYSTEM": "1",
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_PAGER": "cat",
            }
        )
        return environment

    def run(
        self, arguments: Sequence[str], *, input: bytes | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            result = subprocess.run(
                self.command(arguments),
                input=input,
                capture_output=True,
                timeout=self.timeout_seconds,
                env=self.environment(),
                check=False,
            )
        except FileNotFoundError as exc:
            raise GitError("the git executable was not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {arguments[0]} timed out after {self.timeout_seconds}s") from exc
        if check and result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace").strip()
            raise GitError(f"git {arguments[0]} failed: {detail or 'unknown error'}")
        return result

    # -- revisions --------------------------------------------------------------------------

    def resolve_commit(self, revision: str) -> str:
        sha = self.resolve_optional(revision)
        if sha is None:
            raise GitError(f"unable to resolve commit {revision!r}")
        return sha

    def resolve_optional(self, revision: str) -> str | None:
        """Full SHA of ``revision`` as a commit, or None when it is not in this clone."""
        if not revision or "\x00" in revision or revision.startswith("-"):
            return None
        result = self.run(
            ("rev-parse", "--verify", "--quiet", "--end-of-options", f"{revision}^{{commit}}"), check=False
        )
        sha = result.stdout.decode("ascii", "replace").strip()
        return sha if result.returncode == 0 and SHA_RE.fullmatch(sha) else None

    def is_shallow(self) -> bool:
        result = self.run(("rev-parse", "--is-shallow-repository"), check=False)
        return result.stdout.strip() == b"true"

    def first_parent_shas(self, revision: str) -> list[str]:
        """The first-parent chain ending at ``revision``, oldest first: the landing order."""
        sha = self.resolve_commit(revision)
        data = self.run(("rev-list", "--first-parent", "--reverse", sha)).stdout
        return data.decode("ascii").split()

    def identity_names(self, revision: str) -> list[str]:
        """Every author and committer name reachable from ``revision``, sorted and distinct."""
        sha = self.resolve_commit(revision)
        data = self.run(("log", "--format=%an%x00%cn", "-z", sha)).stdout
        return sorted({name for name in data.decode("utf-8", "replace").split("\x00") if name.strip()})

    def missing_objects(self, shas: Sequence[str]) -> set[str]:
        if not shas:
            return set()
        data = self.run(("cat-file", "--batch-check"), input=_lines(shas)).stdout
        return {line.split()[0] for line in data.decode("ascii", "replace").splitlines() if line.endswith(" missing")}

    def remote_url(self) -> str | None:
        """The origin URL without credentials; None for a local-path remote or no remote."""
        result = self.run(("config", "--get", "remote.origin.url"), check=False)
        if result.returncode != 0:
            return None
        return sanitize_url(result.stdout.decode("utf-8", "replace").strip())

    # -- objects ----------------------------------------------------------------------------

    def read_commits(self, shas: Sequence[str]) -> list[CommitObject]:
        """Parse the stored commit objects, in the order given, with one ``cat-file`` process."""
        data = self.run(("cat-file", "--batch"), input=_lines(shas)).stdout
        commits = []
        position = 0
        for sha in shas:
            newline = data.find(b"\n", position)
            if newline < 0:
                raise GitError(f"cat-file output ended before {sha}")
            header = data[position:newline].decode("ascii", "replace").split()
            if len(header) != 3 or header[1] != "commit":
                raise GitError(f"{sha} is not a commit object in this clone")
            size = int(header[2])
            start = newline + 1
            commits.append(_parse_commit(header[0], data[start:start + size]))
            position = start + size + 1
        return commits

    def read_changes(self, shas: Sequence[str]) -> dict[str, tuple[RawChange, ...]]:
        """Changed paths of each commit against its first parent, with one ``git log`` process."""
        data = self.run(
            (
                "log", "--no-walk=unsorted", "--stdin", "-z", "--format=%x01%H",
                "--raw", "--numstat", "--no-abbrev", *DIFF_ARGS,
            ),
            input=_lines(shas),
        ).stdout
        return parse_raw_numstat(data, shas)

    @contextmanager
    def patch_lines(self, shas: Sequence[str]) -> Iterator[Iterator[bytes]]:
        """Stream the ``--unified=0`` patches of ``shas``; each commit opens with a marker line."""
        arguments = (
            "log", "--no-walk=unsorted", "--stdin", "--format=%x00%x01%H",
            "--patch", "--unified=0", *DIFF_ARGS,
        )
        with tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(
                self.command(arguments),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr,
                env=self.environment(),
            )
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(_lines(shas))
            process.stdin.close()
            completed = False
            try:
                yield (line[:-1] if line.endswith(b"\n") else line for line in process.stdout)
                completed = True
            finally:
                if not completed and process.poll() is None:
                    process.kill()
                process.stdout.close()
                returncode = process.wait(timeout=self.timeout_seconds)
                if completed and returncode != 0:
                    stderr.seek(0)
                    detail = stderr.read().decode("utf-8", "replace").strip()
                    raise GitError(f"git log --patch failed: {detail or 'unknown error'}")

    def gitmodules(self, revision: str) -> dict[str, tuple[str, str | None]]:
        """Map submodule path to ``(name, url)`` from ``.gitmodules`` at ``revision``."""
        result = self.run(
            ("config", "--blob", f"{revision}:.gitmodules", "-z", "--get-regexp", r"^submodule\..*\.(path|url)$"),
            check=False,
        )
        if result.returncode != 0:
            return {}
        paths: dict[str, str] = {}
        urls: dict[str, str] = {}
        for entry in result.stdout.split(b"\x00"):
            key_bytes, _, value_bytes = entry.partition(b"\n")
            key = key_bytes.decode("utf-8", "surrogateescape")
            if not key.startswith("submodule."):
                continue
            name, _, attribute = key[len("submodule."):].rpartition(".")
            value = value_bytes.decode("utf-8", "surrogateescape")
            if attribute == "path":
                paths[name] = value
            elif attribute == "url":
                urls[name] = value
        return {path: (name, urls.get(name)) for name, path in paths.items()}

    def blame(self, revision: str, path: str, ranges: Sequence[tuple[int, int]]) -> list[tuple[str, str]]:
        """``(commit, line text)`` for each line in ``ranges`` of ``path`` at ``revision``."""
        arguments = ["blame", "--porcelain", "-w", "-M"]
        for start, end in ranges:
            arguments += ["-L", f"{start},{end}"]
        arguments += [revision, "--", path]
        result = self.run(arguments, check=False)
        if result.returncode != 0:
            return []
        lines: list[tuple[str, str]] = []
        current = None
        for line in result.stdout.split(b"\n"):
            if line.startswith(b"\t"):
                if current is not None:
                    lines.append((current, line[1:].decode("utf-8", "replace")))
                continue
            head = line.split(b" ", 1)[0]
            if len(head) == 40 and SHA_RE.fullmatch(head.decode("ascii", "replace")):
                current = head.decode("ascii")
        return lines


def sanitize_url(url: str) -> str | None:
    """Drop credentials and user names from a remote URL; None for local paths."""
    if "://" in url:
        parts = urlsplit(url)
        if parts.scheme == "file":
            return None
        host = parts.netloc.rpartition("@")[2]
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    scp = re.match(r"^(?:[\w.-]+@)?(?P<host>[\w.-]+):(?P<path>[^/].*)$", url)
    return f"{scp.group('host')}:{scp.group('path')}" if scp else None


def parse_raw_numstat(data: bytes, shas: Sequence[str]) -> dict[str, tuple[RawChange, ...]]:
    """Split ``git log -z --raw --numstat`` output into per-commit change lists."""
    expected = set(shas)
    result: dict[str, tuple[RawChange, ...]] = {}
    tokens = data.split(b"\x00")
    index = 0
    current: str | None = None
    raw: list[tuple] = []
    by_path: dict[str, tuple[int | None, int | None, bool]] = {}
    by_pair: dict[tuple[str, str], tuple[int | None, int | None, bool]] = {}

    def finish() -> None:
        if current is not None:
            result[current] = _join_numstat(current, raw, by_path, by_pair)

    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token.startswith(b"\n"):
            token = token[1:]
        if not token:
            continue
        if token[:1] == b"\x01" and token[1:].decode("ascii", "replace") in expected:
            finish()
            current = token[1:].decode("ascii")
            raw, by_path, by_pair = [], {}, {}
            continue
        if current is None:
            raise GitError("git log output did not start with a commit marker")
        if token[:1] == b":":
            fields = token[1:].decode("ascii", "replace").split(" ")
            if len(fields) != 5 or not fields[4]:
                raise GitError(f"{current}: malformed raw diff entry")
            status = fields[4][0]
            score = int(fields[4][1:]) if fields[4][1:].isdigit() else None
            first = _decode_path(tokens[index])
            index += 1
            if status in "RC":
                old_path, new_path = first, _decode_path(tokens[index])
                index += 1
            elif status == "A":
                old_path, new_path = None, first
            elif status == "D":
                old_path, new_path = first, None
            else:
                old_path = new_path = first
            raw.append((status, old_path, new_path, *fields[:4], score))
            continue
        parts = token.split(b"\t", 2)
        if len(parts) != 3:
            raise GitError(f"{current}: malformed numstat entry")
        binary = parts[0] == b"-" or parts[1] == b"-"
        counts = (None, None, True) if binary else (int(parts[0]), int(parts[1]), False)
        if parts[2]:
            by_path[_decode_path(parts[2])] = counts
        else:
            by_pair[(_decode_path(tokens[index]), _decode_path(tokens[index + 1]))] = counts
            index += 2
    finish()
    missing = [sha for sha in shas if sha not in result]
    for sha in missing:
        result[sha] = ()
    return result


def _join_numstat(
    sha: str,
    raw: list[tuple],
    by_path: dict[str, tuple[int | None, int | None, bool]],
    by_pair: dict[tuple[str, str], tuple[int | None, int | None, bool]],
) -> tuple[RawChange, ...]:
    changes = []
    for status, old_path, new_path, old_mode, new_mode, old_blob, new_blob, score in raw:
        if status in "RC":
            counts = by_pair.get((old_path, new_path))
        else:
            counts = by_path.get(new_path or old_path)
        if counts is None:
            raise GitError(f"{sha}: numstat missing for {new_path or old_path!r}")
        changes.append(
            RawChange(
                status=status,
                old_path=old_path,
                new_path=new_path,
                old_mode=None if old_mode == "000000" else old_mode,
                new_mode=None if new_mode == "000000" else new_mode,
                old_blob=None if old_blob == ZERO_SHA else old_blob,
                new_blob=None if new_blob == ZERO_SHA else new_blob,
                similarity=score,
                additions=counts[0],
                deletions=counts[1],
                binary=counts[2],
            )
        )
    changes.sort(key=lambda change: (change.path, change.old_path or "", change.status))
    return tuple(changes)


def _parse_commit(sha: str, body: bytes) -> CommitObject:
    header, _, message = body.partition(b"\n\n")
    tree = ""
    parents: list[str] = []
    author = committer = b""
    encoding = "utf-8"
    for line in header.split(b"\n"):
        if line.startswith(b" "):
            continue
        key, _, value = line.partition(b" ")
        if key == b"tree":
            tree = value.decode("ascii", "replace")
        elif key == b"parent":
            parents.append(value.decode("ascii", "replace"))
        elif key == b"author":
            author = value
        elif key == b"committer":
            committer = value
        elif key == b"encoding":
            encoding = _codec(value.decode("ascii", "replace"))
    author_name, author_email, authored = _ident(author, encoding)
    committer_name, _, committed = _ident(committer, encoding)
    return CommitObject(
        sha=sha,
        tree=tree,
        parents=tuple(parents),
        author_name=author_name,
        author_email=author_email,
        authored_epoch=authored,
        committer_name=committer_name,
        committed_epoch=committed,
        message=message.decode(encoding, "replace"),
    )


def _ident(value: bytes, encoding: str) -> tuple[str, str, int]:
    text = value.decode(encoding, "replace")
    opening, closing = text.find("<"), text.rfind(">")
    if opening < 0 or closing < opening:
        return text.strip(), "", 0
    fields = text[closing + 1:].split()
    epoch = int(fields[0]) if fields and fields[0].lstrip("-").isdigit() else 0
    return text[:opening].strip(), text[opening + 1:closing].strip(), epoch


def _codec(name: str) -> str:
    try:
        return codecs.lookup(name).name
    except LookupError:
        return "utf-8"


def _decode_path(value: bytes) -> str:
    return value.decode("utf-8", "surrogateescape")


def _lines(shas: Sequence[str]) -> bytes:
    return "".join(f"{sha}\n" for sha in shas).encode("ascii")
