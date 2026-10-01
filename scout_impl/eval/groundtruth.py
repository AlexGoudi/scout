"""Ground truth per incident, derived mechanically from the revert.

The eval question asks whether Scout's uncovered-or-ambiguous set names "the platform or ASIC
family the revert names". The plan had a domain expert decide that; with none available it
is read off the revert by rule, and every entity carries the evidence that produced it:

* a path the revert changes under `device/<vendor>/<platform>/` names that platform, one
  under a shared `_common` directory names every platform linking into it, and one under
  `platform/<family>/` names that family when the tree declares it;
* a vendor, ASIC family, platform or HWSKU name in the revert's subject names that entity.
  A revert's subject quotes the subject of the change it reverts, so this reads both.

Nothing else contributes: not the body, not the cause's own paths and never the brief, so
the label cannot lean on the prediction it grades. Names are matched against a vocabulary
read from the tree the backtest analyzes, so an entity counts only if Scout could have
named it. Matching is whole-word and case-insensitive; five names that are also ordinary
words or routing terms match only inside a bracketed subject tag such as `[Nexthop]`, which
is how SONiC subjects scope themselves.

Vendor names are the vaguest evidence, so they are a fallback. A revert naming a platform
or a family is graded on those; one naming only a vendor is graded on that vendor's
platforms. Otherwise `[Mellanox] Fix SN2700` would count a hit on any Mellanox platform.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

PLATFORM = "platform"
FAMILY = "asic_family"
VENDOR = "vendor"

VIA_PATH = "revert_path"
VIA_SHARED_PATH = "revert_shared_path"
VIA_FAMILY_PATH = "revert_platform_path"
VIA_SUBJECT = "subject"
VIA_SUBJECT_TAG = "subject_tag"

LEVEL_PLATFORM = "platform"
LEVEL_FAMILY = "family"
LEVEL_VENDOR = "vendor"
_LEVELS = (LEVEL_PLATFORM, LEVEL_FAMILY, LEVEL_VENDOR)

STATUS_COVERED = "covered"
STATUS_UNCOVERED = "uncovered"
STATUS_AMBIGUOUS = "ambiguous"
# The headline 88 of 284 reads ambiguous as never built (string equality); the
# architecture-aware reading counts only the uncovered.
NEVER_BUILT = frozenset({STATUS_UNCOVERED, STATUS_AMBIGUOUS})

TAG_ONLY = frozenset({"delta", "fs", "nexthop", "virtual", "vs"})

_TAG_RE = re.compile(r"\[([^\]]+)\]")
_TAG_TOKEN_RE = re.compile(r"[^a-z0-9_.-]+")


@dataclass(frozen=True)
class Vocabulary:
    """What one tree lets Scout name: platforms and their families, vendors, HWSKUs, and coverage."""

    rev: str
    platforms: Dict[str, Tuple[str, ...]]
    families: Tuple[str, ...]
    hwskus: Dict[str, str]
    shared: Dict[str, Tuple[str, ...]]
    status: Dict[str, str] = field(default_factory=dict)

    @property
    def vendors(self) -> Tuple[str, ...]:
        return tuple(sorted({platform.split("/", 1)[0] for platform in self.platforms}))

    @property
    def never_built(self) -> Tuple[str, ...]:
        return tuple(sorted(item for item, state in self.status.items() if state in NEVER_BUILT))

    def platforms_declaring(self, families: Iterable[str]) -> Set[str]:
        wanted = set(families)
        return {platform for platform, declared in self.platforms.items() if wanted & set(declared)}

    def platforms_of(self, vendors: Iterable[str]) -> Set[str]:
        wanted = set(vendors)
        return {platform for platform in self.platforms if platform.split("/", 1)[0] in wanted}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rev": self.rev,
            "platforms": {key: list(value) for key, value in sorted(self.platforms.items())},
            "families": list(self.families),
            "hwskus": dict(sorted(self.hwskus.items())),
            "shared": {key: list(value) for key, value in sorted(self.shared.items())},
            "status": dict(sorted(self.status.items())),
        }


def build_vocabulary(
    index: Any,
    tree_paths: Iterable[str],
    hwsku_markers: Sequence[str],
    model: Any = None,
    coverage: Any = None,
) -> Vocabulary:
    """Read the vocabulary off a static-stage entity index, the tree listing and, if parsed, the pipeline."""
    platforms = {entity.id: tuple(entity.families) for entity in index.entities}
    families: Set[str] = {family for declared in platforms.values() for family in declared}
    if model is not None:
        families |= {group.family for group in model.job_groups} | {group.name for group in model.job_groups}

    hwskus: Dict[str, str] = {}
    for path in tree_paths:
        parts = path.split("/")
        if len(parts) == 5 and parts[0] == index_root(index) and parts[4] in hwsku_markers:
            owner = f"{parts[1]}/{parts[2]}"
            if owner in platforms:
                hwskus.setdefault(parts[3], owner)

    shared = {directory: tuple(index.linking_into(directory)) for directory in index.excluded}

    status: Dict[str, str] = {}
    if coverage is not None:
        for state, members in ((STATUS_COVERED, coverage.covered), (STATUS_UNCOVERED, coverage.uncovered),
                               (STATUS_AMBIGUOUS, coverage.ambiguous)):
            status.update({member: state for member in members})

    return Vocabulary(
        rev=index.rev,
        platforms=platforms,
        families=tuple(sorted(families)),
        hwskus=hwskus,
        shared=shared,
        status=status,
    )


def index_root(index: Any) -> str:
    """The entity root, read off a declaration path rather than assumed to be `device`."""
    for declaration in index.declarations:
        return declaration.path.split("/", 1)[0]
    return "device"


@dataclass(frozen=True)
class GroundTruth:
    """The entities a revert touches or names, each with the evidence that put it there."""

    platforms: Tuple[str, ...] = ()
    families: Tuple[str, ...] = ()
    vendors: Tuple[str, ...] = ()
    evidence: Tuple[Dict[str, str], ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.platforms or self.families or self.vendors)

    @property
    def level(self) -> Optional[str]:
        """The most specific tier the revert names, which is the tier it is graded on."""
        if self.platforms:
            return LEVEL_PLATFORM
        if self.families:
            return LEVEL_FAMILY
        if self.vendors:
            return LEVEL_VENDOR
        return None

    @property
    def entity_ids(self) -> Tuple[str, ...]:
        return tuple(
            [f"{PLATFORM}:{item}" for item in self.platforms]
            + [f"{FAMILY}:{item}" for item in self.families]
            + [f"{VENDOR}:{item}" for item in self.vendors]
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platforms": list(self.platforms),
            "families": list(self.families),
            "vendors": list(self.vendors),
            "level": self.level,
            "evidence": [dict(item) for item in self.evidence],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "GroundTruth":
        return cls(
            platforms=tuple(payload.get("platforms") or ()),
            families=tuple(payload.get("families") or ()),
            vendors=tuple(payload.get("vendors") or ()),
            evidence=tuple(dict(item) for item in payload.get("evidence") or ()),
        )


def extract(subject: str, paths: Sequence[str], vocabulary: Vocabulary) -> GroundTruth:
    """Ground truth for one revert, from its changed paths and its subject."""
    found: Dict[Tuple[str, str], Dict[str, str]] = {}

    def add(kind: str, entity: str, via: str, detail: str) -> None:
        found.setdefault((kind, entity), {"entity": f"{kind}:{entity}", "via": via, "detail": detail})

    for path in sorted(set(paths)):
        parts = path.split("/")
        if len(parts) >= 4 and parts[0] == "device":
            platform = f"{parts[1]}/{parts[2]}"
            directory = "/".join(parts[:3])
            if platform in vocabulary.platforms:
                add(PLATFORM, platform, VIA_PATH, path)
            for linked in vocabulary.shared.get(directory, ()):
                add(PLATFORM, linked, VIA_SHARED_PATH, path)
        elif len(parts) >= 3 and parts[0] == "platform" and parts[1] in vocabulary.families:
            add(FAMILY, parts[1], VIA_FAMILY_PATH, path)

    for kind, entity, via, detail in named_in(subject, vocabulary):
        add(kind, entity, via, detail)

    def of(kind: str) -> Tuple[str, ...]:
        return tuple(sorted(entity for found_kind, entity in found if found_kind == kind))

    return GroundTruth(
        platforms=of(PLATFORM),
        families=of(FAMILY),
        vendors=of(VENDOR),
        evidence=tuple(found[key] for key in sorted(found)),
    )


def named_in(text: str, vocabulary: Vocabulary) -> List[Tuple[str, str, str, str]]:
    """Every vocabulary entity `text` names, as (kind, entity, via, the matched text)."""
    lowered = (text or "").lower()
    tags = [tag.strip() for tag in _TAG_RE.findall(lowered)]
    tag_tokens = {token for tag in tags for token in [tag, *_TAG_TOKEN_RE.split(tag)] if token}

    names: List[Tuple[str, str, str]] = []
    names.extend((VENDOR, vendor, vendor) for vendor in vocabulary.vendors)
    names.extend((FAMILY, family, family) for family in vocabulary.families)
    names.extend((PLATFORM, platform, platform.split("/", 1)[1]) for platform in vocabulary.platforms)
    names.extend((PLATFORM, owner, hwsku) for hwsku, owner in vocabulary.hwskus.items())

    matches: List[Tuple[str, str, str, str]] = []
    for kind, entity, name in names:
        token = name.lower()
        if token in tag_tokens:
            matches.append((kind, entity, VIA_SUBJECT_TAG, f"[{token}]"))
        elif token not in TAG_ONLY and token in lowered and _word(token).search(lowered):
            matches.append((kind, entity, VIA_SUBJECT, name))
    return matches


def mentions(text: str, vocabulary: Vocabulary) -> List[str]:
    """Entity ids `text` names; the relevance filter's reading of a subject or body."""
    return sorted({f"{kind}:{entity}" for kind, entity, _, _ in named_in(text, vocabulary)})


def _word(token: str) -> "re.Pattern[str]":
    # `-` and `_` bind: `Mellanox-SN2700` must not match inside `Mellanox-SN2700-D48C8`, which
    # is a different HWSKU and can be a different platform.
    return re.compile(r"(?<![a-z0-9_-])" + re.escape(token) + r"(?![a-z0-9_-])")


def landing(truth: GroundTruth, vocabulary: Vocabulary) -> Tuple[str, ...]:
    """The platforms a revert lands on, at the most specific tier it names.

    Platforms and families are both first-hand: a path under `platform/<family>/` puts the
    whole family at risk as surely as one under a platform directory puts that platform.
    Vendors count only when nothing more specific was named.
    """
    landed = set(truth.platforms) | vocabulary.platforms_declaring(truth.families)
    if not landed and truth.vendors:
        landed = vocabulary.platforms_of(truth.vendors)
    return tuple(sorted(landed & set(vocabulary.platforms)))


@dataclass(frozen=True)
class Match:
    """Which ground-truth entities a flagged set hits, and at which tier."""

    hits: Tuple[Dict[str, str], ...]

    @property
    def recalled(self) -> bool:
        return bool(self.hits)

    @property
    def level(self) -> Optional[str]:
        levels = {hit["level"] for hit in self.hits}
        return next((level for level in _LEVELS if level in levels), None)

    def to_dict(self) -> Dict[str, Any]:
        return {"recalled": self.recalled, "level": self.level, "hits": [dict(hit) for hit in self.hits]}


def match(truth: GroundTruth, flagged: Iterable[str], families: Mapping[str, Sequence[str]]) -> Match:
    """Grade a flagged platform set against ground truth, on the tier the revert is graded on.

    `flagged` holds brief entity ids (`platform:<vendor>/<name>`) or bare platform ids, and
    `families` maps a bare platform id to the families it declares in the analyzed tree.
    """
    named_platforms, named_families = set(truth.platforms), set(truth.families)
    vendors_count = not (named_platforms or named_families)

    hits: List[Dict[str, str]] = []
    for item in sorted(set(flagged)):
        platform = item.split(":", 1)[1] if item.startswith(f"{PLATFORM}:") else item
        if platform in named_platforms:
            hits.append({"flagged": f"{PLATFORM}:{platform}", "entity": f"{PLATFORM}:{platform}",
                         "level": LEVEL_PLATFORM})
        for family in sorted(set(families.get(platform, ())) & named_families):
            hits.append({"flagged": f"{PLATFORM}:{platform}", "entity": f"{FAMILY}:{family}",
                         "level": LEVEL_FAMILY})
        vendor = platform.split("/", 1)[0]
        if vendors_count and vendor in truth.vendors:
            hits.append({"flagged": f"{PLATFORM}:{platform}", "entity": f"{VENDOR}:{vendor}",
                         "level": LEVEL_VENDOR})
    return Match(hits=tuple(hits))
