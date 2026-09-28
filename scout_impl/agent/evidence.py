"""Pre-assembled evidence: what the brief already names, read once and numbered for citation.

A local 7B model is weak at open-ended, multi-step tool use and good at applying a stated
rule to evidence in front of it, so the agent stage does the looking up itself. For each
question it gathers the three things the D6 contract says a finding rests on (HLD section
4.7) — the diff hunk that is the **cause**, the `platform_asic` lines naming the
**affected** platform's family, and the `azure-pipelines.yml` lines that are the
**contract** — and numbers them `E1`, `E2`, ... The model cites by number, which it does
reliably, rather than by path and line, which it does not.

Every item carries the exact quote it was built from and the revision it was read at, so
the citation resolver can re-read it and drop a finding whose quote no longer matches
(HLD section 4.6). Quotes from the diff are taken from the change set rather than from the
tree, which is what gives that re-read something to catch: a change set and a tree that
disagree about a line are exactly the case where a finding must not be trusted.

A change need not land inside a platform's directory to reach it. Shared directories
exist so platforms can link into them, and a platform directory can itself be a link to a
sibling, so `find_reach_link` finds the symlink a change travelled through and it becomes
`affected` evidence in its own right: the link's target text is its quote, and the
resolver re-reads it like any other line.
"""

import posixpath
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..models import CHANGE_DELETED, LINE_ADDED, LINE_REMOVED, ChangeSet, DiffLine, FileDiff
from ..source import TreeEntry
from ..static.pipeline import item_spans
from .budget import QuestionBudget
from .toolbox import REV_BASE, REV_HEAD, Excerpt, ToolError, Toolbox

ROLE_CAUSE = "cause"
ROLE_AFFECTED = "affected"
ROLE_CONTRACT = "contract"
ROLES = (ROLE_CAUSE, ROLE_AFFECTED, ROLE_CONTRACT)

ORIGIN_TREE = "tree"
ORIGIN_DIFF = "diff"
ORIGIN_BRIEF = "brief"
ORIGIN_LINK = "link"
ORIGIN_LISTING = "listing"

MAX_CAUSE_LINES = 6
MAX_CAUSE_FILES = 3
MAX_DECLARATION_LINES = 4
MAX_LISTING_LINES = 30
MAX_GREP_EVIDENCE = 8


@dataclass(frozen=True)
class Evidence:
    """One citable excerpt, with the role it plays and the exact lines it quotes."""

    id: str
    role: str
    path: str
    rev: str
    line_start: int
    line_end: int
    quote: str
    label: str = ""
    origin: str = "tree"

    def citation(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "rev": self.rev,
            "role": self.role,
            "quote": self.quote,
        }

    @property
    def span(self) -> str:
        return str(self.line_start) if self.line_start == self.line_end else f"{self.line_start}-{self.line_end}"

    def render(self) -> str:
        label = f" ({self.label})" if self.label else ""
        body = "\n".join(f"    {line}" for line in self.quote.splitlines()) or "    (empty)"
        return f"{self.id} [{self.role}] {self.path}:{self.span}{label}\n{body}"


class EvidenceBook:
    """Numbered evidence for one model conversation. Numbering is stable in insertion order."""

    def __init__(self, prefix: str = "E") -> None:
        self.prefix = prefix
        self._items: List[Evidence] = []

    @property
    def items(self) -> Tuple[Evidence, ...]:
        return tuple(self._items)

    def add(self, role: str, path: str, rev: str, line_start: int, line_end: int, quote: str,
            label: str = "", origin: str = "tree") -> Evidence:
        for item in self._items:
            key = (item.role, item.path, item.rev, item.line_start, item.line_end)
            if key == (role, path, rev, line_start, line_end):
                return item
        item = Evidence(id=f"{self.prefix}{len(self._items) + 1}", role=role, path=path, rev=rev,
                        line_start=line_start, line_end=line_end, quote=quote, label=label, origin=origin)
        self._items.append(item)
        return item

    def get(self, evidence_id: str) -> Optional[Evidence]:
        return next((item for item in self._items if item.id == evidence_id), None)

    def render(self) -> str:
        return "\n".join(item.render() for item in self._items)


def platform_directory(entity: Dict[str, Any]) -> str:
    """A platform's directory: the one holding its declaration."""
    return posixpath.dirname(entity["source"])


def platform_name(entity_id: str) -> str:
    return entity_id.split(":", 1)[1] if ":" in entity_id else entity_id


def _first_run(file_diff: FileDiff, kind: str) -> List[DiffLine]:
    """The first contiguous run of `kind` lines, contiguous in the numbering of their own side."""
    for hunk in file_diff.hunks:
        run: List[DiffLine] = []
        for line in hunk.lines:
            number = line.new_lineno if kind == LINE_ADDED else line.old_lineno
            if line.kind == kind and number is not None:
                previous = (run[-1].new_lineno if kind == LINE_ADDED else run[-1].old_lineno) if run else None
                if previous is None or number == previous + 1:
                    run.append(line)
                    continue
            if run:
                return run
        if run:
            return run
    return []


def add_declaration(book: EvidenceBook, toolbox: Toolbox, entity: Dict[str, Any],
                    question: Optional[QuestionBudget] = None, charge: bool = True) -> Optional[Evidence]:
    """The `affected` role: the platform's `platform_asic`, followed through a symlink if it is one."""
    declared = entity["source"]
    try:
        path = toolbox.resolve_path(declared, question=question, charge=charge)
        rows = toolbox.lines(path, question=question, purpose="evidence", charge=charge)
    except ToolError:
        return None
    count = max(1, min(len(rows), MAX_DECLARATION_LINES))
    via = f", via symlink {declared}" if path != declared else ""
    return book.add(ROLE_AFFECTED, path, REV_HEAD, 1, count, "\n".join(rows[:count]),
                    label=f"platform_asic of {platform_name(entity['id'])}{via}")


def add_brief_contract(book: EvidenceBook, rules: Sequence[Dict[str, Any]], rule_id: str,
                       families: Iterable[str]) -> List[Evidence]:
    """The brief's own quoted citations for `rule_id`, narrowed to the groups building `families`.

    A citation naming no family at all is kept: it is the default-architecture contract
    every group is read against.
    """
    wanted = set(families)
    added = []
    for rule in rules:
        if rule["id"] != rule_id:
            continue
        for citation in rule["citations"]:
            quote = citation.get("quote") or ""
            if not quote:
                continue
            named = set(re.findall(r"PLATFORM_NAME:\s*([\w.-]+)", quote))
            if named and not named & wanted:
                continue
            first = quote.splitlines()[0].strip() if quote else ""
            label = f"job group {first.split(':', 1)[1].strip()}" if first.startswith("- name:") else "pipeline default"
            added.append(book.add(ROLE_CONTRACT, citation["path"], citation.get("rev", REV_HEAD),
                                  citation["line_start"], citation["line_end"], quote, label=label, origin="brief"))
    return added


def add_job_group_lines(book: EvidenceBook, toolbox: Toolbox, groups: Sequence[Dict[str, Any]],
                        question: Optional[QuestionBudget] = None, charge: bool = True,
                        whole_items: bool = False) -> List[Evidence]:
    """The `- name:` line of each job group in the pipeline, or its whole item when asked.

    The fallback for a brief that carries no quoted job-group citations, and the compact
    form of "these are all the groups there are" that an uncovered platform's finding cites.
    """
    added = []
    by_path: Dict[str, List[str]] = {}
    for group in groups:
        by_path.setdefault(group.get("source") or "azure-pipelines.yml", []).append(group["id"].split(":", 1)[1])
    for path, names in sorted(by_path.items()):
        try:
            rows = toolbox.lines(path, question=question, purpose="evidence", charge=charge)
        except ToolError:
            continue
        spans = item_spans(rows)
        for name in sorted(names, key=lambda item: spans.get(item, (0, 0))[0]):
            if name not in spans:
                continue
            start, end = spans[name]
            end = end if whole_items else start
            added.append(book.add(ROLE_CONTRACT, path, REV_HEAD, start, end, "\n".join(rows[start - 1:end]),
                                  label=f"job group {name}"))
    return added


@dataclass(frozen=True)
class Reach:
    """How one changed path reaches one platform: inside its directory, through a link, or by the brief's word."""

    path: str
    link: str = ""
    target: str = ""
    via_brief: bool = False


def latest_diffs(change_set: Optional[ChangeSet]) -> Dict[str, FileDiff]:
    """The newest diff of every changed path.

    Newest, because a later commit's hunks are the ones whose line numbers hold at the
    head; an older commit's may not, and the resolver drops what no longer matches.
    """
    latest: Dict[str, FileDiff] = {}
    if change_set is None:
        return latest
    for commit in change_set.commits:
        for file_diff in commit.files:
            latest[file_diff.path] = file_diff
    return latest


def cause_runs(file_diff: FileDiff, max_lines: int = MAX_CAUSE_LINES) -> List[Tuple[str, str, int, int, str]]:
    """The first run added, at the head, and the first run removed, at the base.

    One run is enough to cite a change and not enough to judge one: a line rewritten to mean
    the same thing and a line rewritten to mean something else look alike from the new side
    alone.
    """
    if file_diff.is_binary:
        return []
    runs = []
    for kind, rev in ((LINE_ADDED, REV_HEAD), (LINE_REMOVED, REV_BASE)):
        if file_diff.change_type == CHANGE_DELETED and kind == LINE_ADDED:
            continue
        run = _first_run(file_diff, kind)[:max_lines]
        if run:
            numbers = [line.new_lineno if kind == LINE_ADDED else line.old_lineno for line in run]
            path = file_diff.path if kind == LINE_ADDED else (file_diff.old_path or file_diff.path)
            runs.append((path, rev, numbers[0], numbers[-1], "\n".join(line.content for line in run)))
    return runs


def add_change(book: EvidenceBook, file_diff: FileDiff, label: str = "") -> List[Evidence]:
    """Both runs of one file's change, as `cause` evidence labelled with the change's shape."""
    shape = f"{file_diff.change_type}, +{file_diff.additions} -{file_diff.deletions}"
    added = []
    for path, rev, start, end, quote in cause_runs(file_diff):
        side = "lines added" if rev == REV_HEAD else "lines removed"
        suffix = f"; {label}" if label else ""
        added.append(book.add(ROLE_CAUSE, path, rev, start, end, quote, label=f"{shape}, {side}{suffix}",
                              origin=ORIGIN_DIFF))
    return added


def find_reach_link(toolbox: Toolbox, directory: str, changed_path: str,
                    question: Optional[QuestionBudget] = None, charge: bool = True) -> Optional[Tuple[str, str]]:
    """The symlink through which a change outside `directory` reaches it, as `(link, target text)`.

    Only likely candidates are read, cheapest first: the directory itself when it is an alias
    of a sibling, then links named like the changed file, then links named like one of its
    directories. Identical link texts are one blob, so every platform linking the same shared
    file costs one read between them. None when no candidate lands on the changed path.
    """
    links = toolbox.links_under(directory)
    name = posixpath.basename(changed_path)
    folders = set(changed_path.split("/")[:-1])
    ordered = ([entry.path for entry in links if entry.path == directory]
               + [entry.path for entry in links if entry.path != directory and posixpath.basename(entry.path) == name]
               + [entry.path for entry in links
                  if entry.path != directory and posixpath.basename(entry.path) in folders])
    for link in ordered:
        try:
            landed = toolbox.resolve_path(link, question=question, charge=charge)
        except ToolError:
            continue
        if changed_path == landed or changed_path.startswith(landed + "/"):
            return link, toolbox.content(link, question=question, purpose="symlink", charge=charge).strip()
    return None


def platform_reach(toolbox: Toolbox, entity: Dict[str, Any], diffs: Dict[str, FileDiff],
                   hotspots: Sequence[Dict[str, Any]], question: Optional[QuestionBudget] = None,
                   charge: bool = True) -> List[Reach]:
    """Every changed path that reaches one platform, and how.

    A path the brief's hotspots say reaches the platform, but through no link this can find,
    is kept with `via_brief`: the static stage's reach is a fact of the brief, and dropping it
    here would quietly shrink the question the brief asked.
    """
    directory = platform_directory(entity)
    reached: List[Reach] = []
    for path in sorted(diffs):
        old = diffs[path].old_path or ""
        if path.startswith(directory + "/") or old.startswith(directory + "/"):
            reached.append(Reach(path=path))
            continue
        found = find_reach_link(toolbox, directory, path, question, charge)
        if found:
            reached.append(Reach(path=path, link=found[0], target=found[1]))
    named = {item.path for item in reached}
    for hotspot in hotspots:
        path = hotspot["path"]
        if entity["id"] in (hotspot.get("entities") or []) and path not in named and path in diffs:
            reached.append(Reach(path=path, via_brief=True))
            named.add(path)
    return reached


def reach_signature(entity: Dict[str, Any], reached: Sequence[Reach], diffs: Dict[str, FileDiff]) -> Tuple[Any, ...]:
    """What reached one platform and what it did, independent of the platform's name.

    Inside the directory the path is taken relative to it, so one edit repeated across many
    platforms is one signature; through a link it is the shared path and the link's place in
    the directory, so every platform linking one changed file is one signature too.
    """
    directory = platform_directory(entity)
    signature = []
    for item in sorted(reached, key=lambda reach: reach.path):
        file_diff = diffs[item.path]
        lines = tuple((line.kind, line.content) for hunk in file_diff.hunks for line in hunk.lines
                      if line.kind in (LINE_ADDED, LINE_REMOVED))
        if item.link:
            where = ("link", item.path, item.link[len(directory) + 1:] if item.link != directory else ".")
        elif item.via_brief:
            where = ("brief", item.path)
        else:
            where = ("own", item.path[len(directory) + 1:] if item.path.startswith(directory + "/") else item.path)
        signature.append((where, file_diff.change_type, file_diff.is_binary, lines))
    return tuple(signature)


def add_link(book: EvidenceBook, link: str, target: str, label: str = "") -> Evidence:
    """A symlink as `affected` evidence: its one line is its target, which the resolver re-reads."""
    return book.add(ROLE_AFFECTED, link, REV_HEAD, 1, 1, target, label=label or f"symlink to {target}",
                    origin=ORIGIN_LINK)


def add_excerpt(book: EvidenceBook, excerpt: Excerpt, role: str, label: str = "") -> Evidence:
    return book.add(role, excerpt.path, excerpt.rev, excerpt.line_start, excerpt.line_end, excerpt.text,
                    label=label, origin=ORIGIN_TREE)


def add_listing(book: EvidenceBook, prefix: str, rev: str, entries: Sequence[TreeEntry], total: int,
                role: str = ROLE_AFFECTED) -> Evidence:
    """A directory listing as evidence. Its lines are `mode path`, and it re-resolves by re-listing."""
    shown = list(entries)[:MAX_LISTING_LINES]
    quote = "\n".join(f"{entry.mode} {entry.path}" for entry in shown)
    return book.add(role, prefix.strip("/") or ".", rev, 1, max(1, len(shown)), quote,
                    label=f"listing, {len(shown)} of {total} entries", origin=ORIGIN_LISTING)


def add_grep_hits(book: EvidenceBook, hits: Sequence[Tuple[str, int, str]], rev: str,
                  role: str = ROLE_AFFECTED) -> List[Evidence]:
    return [book.add(role, path, rev, number, number, line, label="grep hit", origin=ORIGIN_TREE)
            for path, number, line in list(hits)[:MAX_GREP_EVIDENCE]]
