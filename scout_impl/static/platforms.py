"""The entity index, and with it counting rules C1 to C4 (HLD section 4.3.1).

Ported from `reference/headline.py`, which produced the figures the documents quote, and
kept in the same order for the same reasons. The order is not cosmetic:

* the `_common` exclusion of C3 happens **before** any declaration is resolved, so the
  three shared directories cost no blob read and cannot contribute a family;
* symlink following of C1 is path arithmetic against the already-listed tree, so
  identifying the 18 links is free and only their targets are read;
* a declaration parses to a **set** under C2, because exactly one of them names two
  families and a string-valued index silently keeps whichever line it saw last;
* owning no HWSKU directory is **recorded and never acted on** under C4, because the
  scan that finds shared directories also finds chassis supervisors and the two must not
  share a fate.

What the index does not do is decide coverage. That is C5 and it lives in `coverage.py`,
because it is a question about a pipeline rather than about the `device/` tree.
"""

import posixpath
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Dict, Iterable, List, Optional, Tuple

from ..repos.base import DirectoryEntitySpec, FileEntitySpec
from .treeindex import SYMLINK_MODE, TreeIndex


class EntityIndexError(RuntimeError):
    """The tree could not be indexed under the adapter's counting rules."""


class SymlinkEscapesTree(EntityIndexError):
    """A declaration symlink resolved outside the repository root.

    An error rather than a best-effort read (HLD section 4.3.3): a target above the root
    is a fact about the tree Scout does not understand, and reading whatever the join
    happens to land on would answer a different question than the one asked.
    """


@dataclass(frozen=True)
class Declaration:
    """One `<root>/<vendor>/<name>/<declaration_file>` path, and what it resolves to.

    Every declaration is resolved, including the ones C3 excludes. `reference/headline.py`
    resolves only the kept ones, which is right for the headline and wrong for the audit:
    the single declaration naming two families **is** `x86_64-arista_common`, so a C2
    count taken over platforms rather than declarations reports zero and the rule looks
    untested. HLD section 4.3.1 states C1's "0 unresolved" over all 287 as well. The extra
    cost is three reads, of which the blob cache absorbs two.
    """

    path: str
    directory: str
    vendor: str
    name: str
    is_symlink: bool
    families: Tuple[str, ...] = ()
    resolved_path: str = ""
    link_hops: int = 0

    @property
    def resolved(self) -> bool:
        return bool(self.families)


@dataclass(frozen=True)
class Entity:
    """One platform: a directory kept by C3 and C4, with the families it declares."""

    id: str
    vendor: str
    name: str
    directory: str
    declaration_path: str
    families: Tuple[str, ...]
    owns_hwsku: bool
    declares_identity: bool
    resolved_path: str
    link_hops: int
    # From the directory name under the adapter's convention, never from a blob; empty
    # when the adapter declares no convention or the name does not follow it.
    arch: str = ""

    @property
    def resolved(self) -> bool:
        return bool(self.families)

    @property
    def resolved_via(self) -> str:
        return "symlink" if self.link_hops else "blob"


class SymlinkGraph:
    """Every symlink under the entity root, and what each one resolves to. Rule C6.

    Built from the tree listing, which gives the mode and the blob sha of every link for
    free, and resolved **lazily and in one batch**. Lazily because a whole-tree run asks no
    reverse-reach question and should pay nothing for one; in one batch because there are
    around 1,850 links holding about 350 distinct targets between them, and a remote source
    charges a round trip per distinct blob. Sequentially that is roughly three minutes. The
    prefetch turns it into a single fetch, and the sha dedupe is what makes 1,850 links cost
    350 reads rather than 1,850.
    """

    def __init__(self, tree: TreeIndex, links: Tuple[Tuple[str, str], ...], max_hops: int) -> None:
        self.tree = tree
        self.links = links
        self.max_hops = max_hops
        self._targets: Optional[Dict[str, str]] = None

    @property
    def resolved(self) -> bool:
        return self._targets is not None

    def resolve_one(self, path: str) -> Optional[str]:
        """Resolve a single link without resolving the rest. Used for the three aliases."""
        return self._follow(path)

    def targets(self) -> Dict[str, str]:
        """Link path to the path it finally resolves to, following chains under the limit."""
        if self._targets is None:
            self.tree.prefetch(path for path, _ in self.links)
            self._targets = {}
            for path, _ in self.links:
                target = self._follow(path)
                if target is not None:
                    self._targets[path] = target
        return self._targets

    def owners_of(self, paths: Iterable[str], depth: int) -> Tuple[str, ...]:
        """Directories at `depth` owning a link that resolves to one of `paths`, or into it.

        "Or into it" is the half that matters. The change that prompted this rule edited
        `device/arista/x86_64-arista_common/pmon_daemon_control.json`, and the 36 links that
        reach it name that exact file; but a link to a whole directory reaches everything
        inside it, so a target is a hit when it equals a changed path **or** is an ancestor
        directory of one.
        """
        wanted = [path for path in paths if path]
        if not wanted:
            return ()

        owners = set()
        for link, target in self.targets().items():
            for path in wanted:
                if target == path or path.startswith(target + "/"):
                    parts = link.split("/")
                    if len(parts) >= depth:
                        owners.add("/".join(parts[:depth]))
                    break
        return tuple(sorted(owners))

    def _follow(self, path: str) -> Optional[str]:
        seen = set()
        current = path
        hops = 0
        while hops <= self.max_hops:
            entry = self.tree.entry(current)
            if entry is None or entry.mode != SYMLINK_MODE:
                return current if current != path else None
            if current in seen:
                return None
            seen.add(current)

            target = posixpath.normpath(posixpath.join(posixpath.dirname(current), self.tree.read(current).strip()))
            if posixpath.isabs(target) or target == ".." or target.startswith("../"):
                raise SymlinkEscapesTree(
                    f"{path} resolves through {current} to {target}, which is above the repository root"
                )
            current = target
            hops += 1
        return None


@dataclass(frozen=True)
class EntityIndex:
    """The closed world of entities the tree declares, with the counting made auditable."""

    rev: str
    kind: str
    family_kind: str
    declarations: Tuple[Declaration, ...]
    entities: Tuple[Entity, ...]
    excluded: Tuple[str, ...]
    blobs_read: int
    cache_hits: int
    # False when the tree declares entities in a shape the counting rules do not describe.
    # The rule-derived counts then report zero, which is the truth — C3 excluded nothing
    # and C4 kept nothing because neither applies — rather than a measurement.
    counting_rules: bool = True
    # C6, outbound: entity directories that are themselves symlinks, by id. They are
    # platforms, and they are deliberately **not** declarations, because they carry no
    # `platform_asic` of their own; `declaration_count` and `len(entities)` are different
    # quantities and the brief reports both.
    aliases: Tuple[str, ...] = ()
    # C6, inbound. None when the adapter does not declare that this tree shares data by
    # symlink, which is also what keeps a whole-tree run from paying for a resolution it
    # has no changed path to use.
    links: Optional[SymlinkGraph] = None
    # Path components in an entity directory, so `reaching` can name the owner of a link
    # without guessing at the tree's shape.
    entity_depth: int = 3

    @property
    def declaration_count(self) -> int:
        """C3's left-hand side: what a reader who globs the tree and counts lines gets.

        Not the platform count, and the two coincide numerically on upstream at
        `62cfe5086` — 287 declarations, 287 platforms — for unrelated reasons: C3 takes
        three away and C6 puts three different ones back.
        """
        return len(self.declarations)

    @property
    def symlink_declarations(self) -> Tuple[Declaration, ...]:
        return tuple(item for item in self.declarations if item.is_symlink)

    @property
    def unresolved(self) -> Tuple[Declaration, ...]:
        """C1's regression signal. Zero upstream on 21 Sep 2026, so any entry is news."""
        return tuple(item for item in self.declarations if not item.resolved)

    @property
    def multi_family(self) -> Tuple[Declaration, ...]:
        """C2's evidence, counted over declarations because the one case is a C3 exclusion."""
        return tuple(item for item in self.declarations if len(item.families) > 1)

    @property
    def kept_without_hwsku(self) -> Tuple[Entity, ...]:
        """C4 made visible: the supervisors and fabric cards the obvious rule would drop."""
        if not self.counting_rules:
            return ()
        return tuple(entity for entity in self.entities if not entity.owns_hwsku)

    @property
    def families(self) -> Dict[str, Tuple[str, ...]]:
        """Family name to the ids of the entities declaring it, in stable order."""
        grouped: Dict[str, List[str]] = {}
        for entity in self.entities:
            for family in entity.families:
                grouped.setdefault(family, []).append(entity.id)
        return {family: tuple(sorted(ids)) for family, ids in sorted(grouped.items())}

    def by_id(self, entity_id: str) -> Optional[Entity]:
        return next((entity for entity in self.entities if entity.id == entity_id), None)

    def owning(self, path: str) -> Optional[Entity]:
        """The entity whose directory contains `path`, or None if no entity does."""
        for entity in self.entities:
            if path == entity.directory or path.startswith(entity.directory + "/"):
                return entity
        return None

    def linking_into(self, path: str) -> Tuple[str, ...]:
        """Entities whose declaration resolves through a directory containing `path`.

        The narrow case C1 and C3 share, kept because it answers for a bare directory path
        that `reaching` cannot: a link names a file, so a changed *directory* is neither
        equal to a target nor underneath one. A diff never names a bare directory, so in
        practice `reaching` subsumes this; the union of the two is what callers use.
        """
        for directory in self.excluded:
            if path == directory or path.startswith(directory + "/"):
                return tuple(sorted(
                    entity.id for entity in self.entities
                    if entity.resolved_path.startswith(directory + "/")
                ))
        return ()

    def reaching(self, paths: Iterable[str]) -> Tuple[str, ...]:
        """Rule C6, inbound: entities owning a symlink that resolves to one of these paths.

        The defect this closes is the one Scout exists to catch. A change to
        `device/arista/x86_64-arista_common/pmon_daemon_control.json` is the pmon
        configuration of 36 real Arista platforms, and reported zero before this rule,
        because no platform *owns* that path and no platform's `platform_asic` resolves
        through it. One shared edit, dozens of boxes, silently.
        """
        if self.links is None:
            return ()
        directories = self.links.owners_of(paths, self.entity_depth)
        by_directory = {entity.directory: entity.id for entity in self.entities}
        return tuple(sorted(by_directory[item] for item in directories if item in by_directory))


def build_entity_index(tree: TreeIndex, spec: DirectoryEntitySpec) -> EntityIndex:
    """Index the tree under counting rules C1 to C4. Tree-first; blobs only for C1 and C2."""
    declarations: List[Declaration] = []
    owns_hwsku = set()
    declares_identity = set()
    prefix = spec.root + "/"

    for path, entry in tree.entries.items():
        if not path.startswith(prefix):
            continue
        parts = path.split("/")
        if len(parts) < 4:
            continue
        directory = "/".join(parts[:3])

        if len(parts) == 4 and parts[3] == spec.declaration_file:
            declarations.append(
                Declaration(
                    path=path,
                    directory=directory,
                    vendor=parts[1],
                    name=parts[2],
                    is_symlink=entry.mode == SYMLINK_MODE,
                )
            )
        elif len(parts) == 4 and spec.identity_marker and parts[3] == spec.identity_marker:
            declares_identity.add(directory)
        elif len(parts) >= 5 and parts[-1] in spec.hwsku_markers:
            owns_hwsku.add(directory)

    declarations.sort(key=lambda item: item.path)
    before_reads, before_hits = tree.blob_reads, tree.cache_hits

    # One batched fetch for every declaration before any of them is read. The symlinked
    # ones resolve to other declarations, so this covers their targets too, and against a
    # blob-filtered remote it is the difference between one round trip and thirty-four.
    tree.prefetch(item.path for item in declarations)

    # C1 and C2 over every declaration, so both are auditable on the 287 the document
    # counts rather than on the 284 that survive C3.
    resolved = [_resolved(tree, item, spec.max_link_hops) for item in declarations]

    # C3: a shared directory is not hardware, so it is not an entity. It keeps its
    # resolution above — the audit needs it — and contributes no platform below.
    excluded = tuple(item.directory for item in resolved if item.name.endswith(spec.shared_suffix))
    shared = set(excluded)

    entities = [
        Entity(
            id=f"{item.vendor}/{item.name}",
            vendor=item.vendor,
            name=item.name,
            directory=item.directory,
            declaration_path=item.path,
            families=item.families,
            owns_hwsku=item.directory in owns_hwsku,
            declares_identity=item.directory in declares_identity,
            resolved_path=item.resolved_path,
            link_hops=item.link_hops,
            arch=spec.arch_of(item.name),
        )
        for item in resolved
        if item.directory not in shared
    ]

    # C6. The links come from the listing and cost nothing; resolving them costs blob
    # reads, so only the three alias directories are resolved here and the rest wait until
    # something asks a reverse-reach question.
    links = SymlinkGraph(tree, _link_entries(tree, prefix), spec.max_link_hops)
    aliases = _alias_entities(links, entities, spec) if spec.alias_directories else ()
    entities.extend(aliases)
    entities.sort(key=lambda entity: entity.id)

    return EntityIndex(
        rev=tree.rev,
        kind=spec.kind,
        family_kind=spec.family_kind,
        declarations=tuple(resolved),
        entities=tuple(entities),
        excluded=tuple(sorted(excluded)),
        blobs_read=tree.blob_reads - before_reads,
        cache_hits=tree.cache_hits - before_hits,
        aliases=tuple(entity.id for entity in aliases),
        links=links if spec.reverse_reach else None,
        entity_depth=len(spec.root.split("/")) + 2,
    )


def _link_entries(tree: TreeIndex, prefix: str) -> Tuple[Tuple[str, str], ...]:
    """Every symlink under the entity root, as (path, blob sha). Listing only, no reads."""
    return tuple(sorted(
        (path, entry.sha) for path, entry in tree.entries.items()
        if entry.mode == SYMLINK_MODE and path.startswith(prefix)
    ))


def _alias_entities(links: SymlinkGraph, entities: List[Entity], spec: DirectoryEntitySpec) -> Tuple[Entity, ...]:
    """Rule C6, outbound: an entity directory that is itself a symlink to another one.

    Each is a distinct ONIE platform name and a real deployable box, running the data of
    the directory it points at, so it inherits that directory's declaration wholesale — its
    families, its HWSKU ownership, the path the families were actually read from — and keeps
    only what is its own: its name, and therefore the architecture that name implies.
    """
    by_directory = {entity.directory: entity for entity in entities}
    depth = len(spec.root.split("/")) + 2

    candidates = [path for path, _ in links.links if len(path.split("/")) == depth]
    links.tree.prefetch(candidates)

    aliased = []
    for path in candidates:
        parts = path.split("/")
        target = links.resolve_one(path)
        source = by_directory.get(target or "")
        if source is None:
            continue
        aliased.append(
            Entity(
                id=f"{parts[depth - 2]}/{parts[depth - 1]}",
                vendor=parts[depth - 2],
                name=parts[depth - 1],
                directory=path,
                declaration_path=source.declaration_path,
                families=source.families,
                owns_hwsku=source.owns_hwsku,
                declares_identity=source.declares_identity,
                resolved_path=source.resolved_path,
                link_hops=source.link_hops + 1,
                arch=spec.arch_of(parts[depth - 1]),
            )
        )
    return tuple(aliased)


def build_file_entity_index(tree: TreeIndex, spec: FileEntitySpec) -> EntityIndex:
    """Index a tree whose entities are files, not directories. Listing only, no blob reads.

    The second shape, added for `sonic-mgmt`. Nothing here resolves, excludes or keeps
    anything: the counting rules are a property of how `sonic-buildimage` declares a
    platform, and pretending they apply would produce three zeros that look like measured
    facts. They are absent instead, and the brief reports the absence.
    """
    pattern = re.compile(spec.name_pattern)
    entities: List[Entity] = []
    declarations: List[Declaration] = []

    for path in sorted(tree.entries):
        if not fnmatchcase(path, spec.glob):
            continue
        matched = pattern.match(path)
        if not matched:
            continue

        name = matched.group("name")
        directory = posixpath.dirname(path)
        declarations.append(
            Declaration(path=path, directory=directory, vendor="", name=name, is_symlink=False,
                        families=(name,), resolved_path=path)
        )
        entities.append(
            Entity(
                id=name,
                vendor="",
                name=name,
                directory=path,
                declaration_path=path,
                families=(name,),
                owns_hwsku=False,
                declares_identity=False,
                resolved_path=path,
                link_hops=0,
            )
        )

    return EntityIndex(
        rev=tree.rev,
        kind=spec.kind,
        family_kind=spec.family_kind,
        declarations=tuple(declarations),
        entities=tuple(entities),
        excluded=(),
        blobs_read=0,
        cache_hits=0,
        counting_rules=False,
    )


def _resolved(tree: TreeIndex, declaration: Declaration, max_hops: int) -> Declaration:
    families, resolved_path, hops = _resolve(tree, declaration.path, max_hops)
    return Declaration(
        path=declaration.path,
        directory=declaration.directory,
        vendor=declaration.vendor,
        name=declaration.name,
        is_symlink=declaration.is_symlink,
        families=families,
        resolved_path=resolved_path,
        link_hops=hops,
    )


def _resolve(tree: TreeIndex, path: str, max_hops: int) -> Tuple[Tuple[str, ...], str, int]:
    """Read a declaration, following symlinks by path arithmetic. Rule C1.

    Returns the families it names, the path they were read from, and how many links were
    followed. An empty family tuple means unresolved — a dangling target, a cycle, or a
    chain longer than the limit — which is recorded against the entity rather than
    dropping it, because a disappearing platform is exactly the failure C4 is about.
    """
    seen = set()
    current = path
    hops = 0

    while current not in seen:
        seen.add(current)
        entry = tree.entry(current)
        if entry is None:
            return (), current, hops

        body = tree.read(current)
        if entry.mode != SYMLINK_MODE:
            return _parse_families(body), current, hops
        if hops >= max_hops:
            return (), current, hops

        target = posixpath.normpath(posixpath.join(posixpath.dirname(current), body.strip()))
        if posixpath.isabs(target) or target == ".." or target.startswith("../"):
            raise SymlinkEscapesTree(
                f"{path} resolves through {current} to {target}, which is above the repository root"
            )
        current = target
        hops += 1

    return (), current, hops


def _parse_families(body: str) -> Tuple[str, ...]:
    """Rule C2: one family per line, parsed to a set. Never a string."""
    return tuple(sorted({line.strip() for line in body.splitlines() if line.strip()}))
