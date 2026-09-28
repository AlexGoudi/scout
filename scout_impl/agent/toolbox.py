"""The agent's read-only tools over the fetched tree (HLD section 4.5).

Every tool reads through the same `RepoSource` stage 0 fetched, so the agent reaches no
network the fetch did not already reach and can neither write nor execute anything. The
costs follow the source's own asymmetry: listings answer from tree metadata and are free,
file contents are round trips and are charged.

| Tool | Cost |
| --- | --- |
| `read_blob(path, rev, start, end)` | One read per distinct blob, charged to the budget; at most `MAX_READ_LINES` |
| `list_tree(prefix, rev)` | Free: the listing is taken once per revision and answered from memory |
| `grep(pattern, glob, rev)` | Charged per blob touched; refused up front if the glob matches over `MAX_GREP_FILES` |
| `entity_query(id, relation)` | Free and deterministic, over the brief's closed world; an id outside it is an error |

`git_log` and `git_blame` are deliberately not offered. Stage 0 fetches the head at depth
2 with `--filter=blob:none` and the base side as bare commits, so history is one or two
commits deep and blame would attribute every line to the shallow boundary commit. A tool
that answers "nobody wrote this" with confidence is worse than no tool.

Reads are memoized on the blob sha, like the static stage's `TreeIndex`, and every path a
tool result depended on is recorded in `consulted`, which is what lets a live run be pinned
into a fixture that replays the identical tool results offline.
"""

import posixpath
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from ..core.runlog import RunLog
from ..gitcmd import GitError
from ..source import RepoSource, TreeEntry
from ..static.fixtures import FixtureError
from .budget import BLOB_READS, Budget, QuestionBudget

REV_HEAD = "head"
REV_BASE = "base"

SYMLINK_MODE = "120000"
MAX_READ_LINES = 60
MAX_LIST_ENTRIES = 60
MAX_GREP_FILES = 6
MAX_GREP_HITS = 20
MAX_PATTERN_LENGTH = 200
MAX_LINK_HOPS = 8

SOURCE_ERRORS = (GitError, FixtureError, OSError)


class ToolError(RuntimeError):
    """A tool call could not be answered. Returned to the model as an observation, never raised past it."""


@dataclass(frozen=True)
class Excerpt:
    """Lines `line_start..line_end` of one file at one revision, exactly as the tree holds them."""

    path: str
    rev: str
    commit: str
    line_start: int
    line_end: int
    lines: Tuple[str, ...]
    total_lines: int

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class Toolbox:
    """Read-only access to the tree at the brief's two revisions, with costs charged and logged."""

    def __init__(
        self,
        source: RepoSource,
        head_sha: str,
        base_sha: str,
        entities: Sequence[Dict[str, Any]],
        budget: Budget,
        run_log: RunLog,
    ) -> None:
        self.source = source
        self.head_sha = head_sha
        self.base_sha = base_sha
        self.entities = {entity["id"]: entity for entity in entities}
        self.budget = budget
        self.run_log = run_log
        self.consulted: Dict[str, Set[str]] = {}
        self.blob_reads = 0
        self.cache_hits = 0
        self._listings: Dict[str, Dict[str, TreeEntry]] = {}
        self._links: Dict[str, List[TreeEntry]] = {}
        self._blobs: Dict[str, str] = {}

    def commit(self, rev: str) -> str:
        """`head` and `base` name the brief's revisions; anything else must be one of them."""
        if rev in (REV_HEAD, self.head_sha):
            return self.head_sha
        if rev in (REV_BASE, self.base_sha):
            if not self.base_sha:
                raise ToolError("this brief has no base revision: it was built over a whole tree, not a change")
            return self.base_sha
        raise ToolError(f"revision {rev!r} is not one the brief names; use 'head' or 'base'")

    def symbolic(self, commit: str) -> str:
        return REV_BASE if commit == self.base_sha and commit != self.head_sha else REV_HEAD

    def listing(self, rev: str) -> Dict[str, TreeEntry]:
        commit = self.commit(rev)
        if commit not in self._listings:
            try:
                entries = self.source.list_tree(commit)
            except SOURCE_ERRORS as error:
                raise ToolError(f"the tree at {self.symbolic(commit)} ({commit[:9]}) cannot be listed: "
                                f"{error}") from None
            self._listings[commit] = {entry.path: entry for entry in entries}
        return self._listings[commit]

    def entry(self, path: str, rev: str = REV_HEAD) -> Optional[TreeEntry]:
        return self.listing(rev).get(path)

    def links_under(self, prefix: str, rev: str = REV_HEAD) -> List[TreeEntry]:
        """Symlink entries at or under `prefix`, from an index of the listing's links. Free."""
        commit = self.commit(rev)
        if commit not in self._links:
            self._links[commit] = [entry for _, entry in sorted(self.listing(rev).items())
                                   if entry.mode == SYMLINK_MODE]
        clean = prefix.strip("/")
        return [entry for entry in self._links[commit] if entry.path == clean or entry.path.startswith(clean + "/")]

    def resolve_path(self, path: str, rev: str = REV_HEAD, question: Optional[QuestionBudget] = None,
                     charge: bool = True) -> str:
        """Follow symlinks in any component of `path`, directory links included (rule C1).

        A platform directory can itself be a link to a sibling, and then nothing under it is
        in the listing under its own name, so each component is checked in turn and the walk
        restarts from wherever a link lands.
        """
        parts = [part for part in path.split("/") if part]
        index, hops, current = 0, 0, ""
        while index < len(parts):
            candidate = f"{current}/{parts[index]}" if current else parts[index]
            entry = self.entry(candidate, rev)
            if entry is not None and entry.mode == SYMLINK_MODE:
                hops += 1
                if hops > MAX_LINK_HOPS:
                    raise ToolError(f"{path} passes through a chain of more than {MAX_LINK_HOPS} links")
                target = self.content(candidate, rev, question, purpose="symlink", charge=charge).strip()
                landed = posixpath.normpath(posixpath.join(posixpath.dirname(candidate), target))
                if landed.startswith("../") or landed == ".." or posixpath.isabs(landed):
                    raise ToolError(f"{path} links outside the repository")
                parts = landed.split("/") + parts[index + 1:]
                index, current = 0, ""
                continue
            current = candidate
            index += 1
        return current

    def content(
        self,
        path: str,
        rev: str = REV_HEAD,
        question: Optional[QuestionBudget] = None,
        purpose: str = "",
        charge: bool = True,
    ) -> str:
        """The whole file, from the blob cache when it holds the bytes, else one charged read."""
        commit = self.commit(rev)
        entry = self.listing(rev).get(path)
        if entry is None or not entry.is_file:
            raise ToolError(f"{path} is not a file in the tree at {self.symbolic(commit)} ({commit[:9]})")
        self._consult(commit, path)

        cached = self._blobs.get(entry.sha)
        if cached is not None:
            self.cache_hits += 1
            return cached

        if charge:
            self.budget.charge(BLOB_READS, 1, question)
        try:
            text = self.source.read_file(commit, path)
        except SOURCE_ERRORS as error:
            raise ToolError(f"{path} at {self.symbolic(commit)} cannot be read: {error}") from None
        self._blobs[entry.sha] = text
        self.blob_reads += 1
        self.run_log.emit("blob_read", path=path, rev=self.symbolic(commit), sha=entry.sha, purpose=purpose,
                          question=question.question if question else None, charged=charge)
        return text

    def lines(self, path: str, rev: str = REV_HEAD, question: Optional[QuestionBudget] = None,
              purpose: str = "", charge: bool = True) -> List[str]:
        return self.content(path, rev, question, purpose, charge).splitlines()

    def read_blob(
        self,
        path: str,
        rev: str = REV_HEAD,
        start: int = 1,
        end: Optional[int] = None,
        question: Optional[QuestionBudget] = None,
        purpose: str = "tool",
        charge: bool = True,
    ) -> Excerpt:
        rows = self.lines(path, rev, question, purpose, charge)
        start = max(1, int(start or 1))
        end = len(rows) if end is None or int(end) <= 0 else min(int(end), len(rows))
        end = min(end, start + MAX_READ_LINES - 1)
        if start > len(rows):
            raise ToolError(f"{path} has {len(rows)} line(s); line {start} does not exist")
        commit = self.commit(rev)
        return Excerpt(path=path, rev=self.symbolic(commit), commit=commit, line_start=start, line_end=end,
                       lines=tuple(rows[start - 1:end]), total_lines=len(rows))

    def list_tree(self, prefix: str, rev: str = REV_HEAD) -> Tuple[List[TreeEntry], int]:
        """Entries under `prefix`, capped, with the uncapped count. Free."""
        commit = self.commit(rev)
        clean = (prefix or "").strip().strip("/")
        entries = [entry for path, entry in sorted(self.listing(rev).items())
                   if not clean or path == clean or path.startswith(clean + "/")]
        for entry in entries:
            self._consult(commit, entry.path)
        return entries[:MAX_LIST_ENTRIES], len(entries)

    def grep(self, pattern: str, glob: str, rev: str = REV_HEAD,
             question: Optional[QuestionBudget] = None) -> Tuple[List[Tuple[str, int, str]], int]:
        """Lines matching `pattern` in files matching `glob`. Charged per blob, refused when too broad."""
        if not pattern or len(pattern) > MAX_PATTERN_LENGTH:
            raise ToolError(f"grep needs a pattern of 1 to {MAX_PATTERN_LENGTH} characters")
        if not glob:
            raise ToolError("grep needs a glob naming the files to search, such as device/<vendor>/<platform>/*")
        commit = self.commit(rev)
        files = [path for path, entry in sorted(self.listing(rev).items())
                 if entry.is_file and fnmatchcase(path, glob)]
        for path in files:
            self._consult(commit, path)
        if not files:
            raise ToolError(f"no file in the tree matches {glob!r}")
        if len(files) > MAX_GREP_FILES:
            raise ToolError(f"{glob!r} matches {len(files)} files; narrow it to at most {MAX_GREP_FILES}")

        try:
            matcher = re.compile(pattern)
        except re.error:
            matcher = re.compile(re.escape(pattern))
        hits: List[Tuple[str, int, str]] = []
        for path in files:
            for number, line in enumerate(self.lines(path, rev, question, purpose="grep"), start=1):
                if matcher.search(line):
                    hits.append((path, number, line))
                    if len(hits) >= MAX_GREP_HITS:
                        return hits, len(files)
        return hits, len(files)

    def entity_query(self, entity_id: str, relation: str = "entity") -> Dict[str, Any]:
        """Traverse the brief's closed world. Deterministic and free; an unknown id is an error."""
        entity = self.entities.get(entity_id)
        if entity is None:
            raise ToolError(f"{entity_id!r} is not in the brief's closed world, so nothing may be said about it")

        if relation in ("entity", ""):
            return dict(entity)
        if relation == "declaration":
            return {"entity": entity_id, "declaration": entity.get("source", "")}
        if relation == "arch":
            return {"entity": entity_id, "arch": entity.get("arch", "unknown"),
                    "derivation": "directory-name prefix (BI-R4, a naming convention)"}
        if relation == "families":
            if entity["kind"] != "platform":
                raise ToolError(f"relation 'families' applies to a platform, not a {entity['kind']}")
            return {"entity": entity_id, "families": list(self.declared_families(entity))}
        if relation == "job_groups":
            families = ([entity_id.split(":", 1)[1]] if entity["kind"] == "asic_family"
                        else list(self.declared_families(entity)) if entity["kind"] == "platform" else [])
            groups = [
                {"id": item["id"], "family": item.get("family") or item["id"].split(":", 1)[1],
                 "arch": item.get("arch", "")}
                for item in self.entities.values()
                if item["kind"] == "ci_job_group" and (item.get("family") or item["id"].split(":", 1)[1]) in families
            ]
            return {"entity": entity_id, "job_groups": sorted(groups, key=lambda item: item["id"])}
        raise ToolError(f"unknown relation {relation!r}; use entity, declaration, arch, families or job_groups")

    def declared_families(self, entity: Dict[str, Any]) -> Tuple[str, ...]:
        """Families a platform declares, read through the blob cache. Uncharged: the static stage paid for it."""
        path = self.resolve_path(entity["source"], charge=False)
        return tuple(sorted({line.strip() for line in self.lines(path, purpose="entity_query", charge=False)
                             if line.strip()}))

    def _consult(self, commit: str, path: str) -> None:
        self.consulted.setdefault(commit, set()).add(path)
