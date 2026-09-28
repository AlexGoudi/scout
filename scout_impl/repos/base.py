"""The repo adapter boundary: everything Scout knows about one repository it analyzes.

Scout reads more than one SONiC repository, and almost all of what differs between them
is knowledge rather than logic: which directories carry which kind of risk, where the
graph entities live, what the repository's own CI actually exercises. An adapter is
therefore a declarative table rather than a subclass — `RepoAdapter` is a frozen
dataclass, and supporting a new repository means writing its rules down in a module
under this package and registering it, not implementing an interface.

Path classes are adapter-scoped. `ansible_code` is meaningless in `sonic-buildimage` and
`platform_data` is meaningless in `sonic-mgmt`, so one shared enum would be wrong for
both. What is shared is `PathKind`, the repo-neutral semantics every class maps onto,
and `PathClass.rank`, the blast-radius order: the prefilter sorts on `rank` and groups on
`kind`, and never asks which repository it is looking at.
"""

from dataclasses import dataclass
from enum import Enum
from fnmatch import fnmatchcase
from typing import Any, Callable, Mapping, Optional, Tuple, Union


class RepoAdapterError(RuntimeError):
    """An adapter could not be built, found or identified."""


class PathKind(str, Enum):
    """Repo-neutral semantics every adapter's path classes map onto.

    Deliberately coarse. It exists so cross-repo logic can ask "is this documentation?"
    or "is this build configuration?" without a table of per-repo class ids; anything
    finer belongs in the adapter's own classes.
    """

    CODE = "code"
    DATA = "data"
    TEST = "test"
    BUILD = "build"
    DOCUMENTATION = "documentation"
    OTHER = "other"


@dataclass(frozen=True)
class PathClass:
    """One repository's blast-radius class for a changed path.

    `rank` is 1 for the widest blast radius and increases as the radius narrows. Ranks
    are assigned on the same scale in every adapter, which is what lets the prefilter
    rank a change set without knowing its repository.
    """

    repo: str
    id: str
    rank: int
    kind: PathKind
    description: str = ""

    @property
    def qualified_id(self) -> str:
        """`repo:id`. A serialized change set carries this so a `FileDiff` read back out
        of the cache is interpretable on its own."""
        return f"{self.repo}:{self.id}"

    def __str__(self) -> str:
        return self.qualified_id


# Evaluated in order, first match wins; `*` spans directory separators.
PathRule = Tuple[str, PathClass]


@dataclass(frozen=True)
class EntitySource:
    """Where one kind of knowledge-graph entity lives in the tree (FR-4).

    Declarative so the extractor can enumerate candidates from a tree listing alone,
    which is what keeps indexing a remotely fetched repository free of blob transfers.
    The extractor itself is separate work; an adapter only says where to look.
    """

    kind: str
    globs: Tuple[str, ...]
    description: str = ""


@dataclass(frozen=True)
class InvariantSpec:
    """An unwritten contract the detectors check, named by the adapter that knows it.

    Empty on both adapters today. The detector catalog is separate work, and this is the
    table it populates rather than standing up a second registry beside this one.
    """

    id: str
    title: str
    question: str
    applies_to: Tuple[PathKind, ...] = ()


@dataclass(frozen=True)
class CiSurface:
    """One thing the repository's own CI exercises, and the file that decides it.

    The coverage gap (FR-5) is affected artifacts minus what CI covers, so an adapter has
    to be able to point at where that set is declared. It points rather than copies: the
    upstream list drifts, and a duplicated list would drift with it silently.
    """

    name: str
    config_path: str
    description: str = ""


@dataclass(frozen=True)
class DirectoryEntitySpec:
    """Where a repository's primary graph entity lives, and how to count it.

    The shape is "a directory under `root` is an entity when it carries
    `declaration_file`, whose contents name the families that entity belongs to". It
    carries the counting rules of HLD section 4.3.1 as data rather than as code, because
    those rules are the committed detector's specification and a reader has to be able to
    check them against the document without reading an extractor: `shared_suffix` is C3,
    `hwsku_markers` is what C4 refuses to exclude on, and `max_link_hops` bounds C1.
    """

    kind: str
    family_kind: str
    root: str
    declaration_file: str
    shared_suffix: str
    hwsku_markers: Tuple[str, ...] = ()
    identity_marker: str = ""
    max_link_hops: int = 8
    # A naming convention, not a declaration: the directory-name prefix before
    # `arch_separator`, mapped to the CPU architecture the pipeline names. Empty means the
    # repository has no such convention and entities carry no architecture at all.
    arch_prefixes: Tuple[Tuple[str, str], ...] = ()
    arch_separator: str = "-"
    # Rule C6, both halves. `alias_directories` says an entity directory may itself be a
    # git symlink to another one, making a second ONIE platform out of one set of data.
    # `reverse_reach` says a change to a path reaches every entity owning a symlink that
    # resolves to it, which is how a shared directory's contents belong to the platforms
    # that link into them. Both are off unless a repository declares it shares data this
    # way, and both follow chains under `max_link_hops`, the same bound as C1.
    alias_directories: bool = False
    reverse_reach: bool = False

    def arch_of(self, name: str) -> str:
        """The architecture a directory name implies under the convention, or ''."""
        prefix = name.split(self.arch_separator, 1)[0] if self.arch_separator in name else ""
        return dict(self.arch_prefixes).get(prefix, "")


@dataclass(frozen=True)
class FileEntitySpec:
    """Where a repository's primary entity is a **file** rather than a directory.

    Added for the second adapter and recorded as such. `sonic-buildimage` declares a
    platform by putting a `platform_asic` file inside a directory; `sonic-mgmt` declares a
    topology by the existence of `ansible/vars/topo_<name>.yml`, with the name in the file
    name and nothing inside it to read. The counting rules of `DirectoryEntitySpec` have no
    analogue here — there are no symlinked declarations, no shared directories and no
    multi-valued families — so this is a second shape rather than a parameterisation of the
    first, and the entity index it produces reports zeros for the rules that do not apply.
    """

    kind: str
    family_kind: str
    glob: str
    name_pattern: str


@dataclass(frozen=True)
class ConstantCoverageSpec:
    """Where CI coverage is declared as a Python constant rather than a pipeline stage.

    Also added for the second adapter. `sonic-mgmt` names the topologies its PR checkers
    run in `PR_TOPOLOGY_TYPE`, a list literal in
    `.azure-pipelines/impacted_area_testing/constant.py`, with a second constant mapping
    each checker to the topology it actually runs. That mapping is **definitive**, not
    architecture-qualified, which is why a job group only becomes ambiguous when it carries
    a qualifier of its own.
    """

    model: str
    path: str
    constant: str
    strip_suffix: str = ""
    alias_constant: str = ""


@dataclass(frozen=True)
class PipelineCoverageSpec:
    """Which stages of which pipeline definition decide what PR CI actually builds.

    `stages` is the strict scope and every name in it must be present, because falling
    back to a whole-file scan when a stage is missing is the silent failure HLD section
    6.2.2 is written about. `family_variable` is the job-group variable naming the
    unqualified family an architecture-qualified group builds, which is what turns the
    `marvell-prestera-arm64` ambiguity into data instead of a hardcoded pair of names.
    """

    model: str
    path: str
    stages: Tuple[str, ...]
    group_parameter: str = "jobGroups"
    family_variable: str = ""
    arch_variable: str = ""
    default_arch: str = ""
    max_template_depth: int = 8
    max_templates: int = 24


@dataclass(frozen=True)
class GitHubApiSpec:
    """GitHub REST metadata for calibration collectors."""

    owner: str
    repo: str
    ref: str = "origin/master"
    user_agent: str = "sonic-scout"


@dataclass(frozen=True)
class AzureDevOpsSpec:
    """Azure DevOps pipeline definition and paging limits for PR timelines."""

    org_url: str
    pipeline_name: str
    definition_id: int
    pr_builds: int = 3000
    pr_timelines: int = 800
    builds_per_pr: int = 3
    yaml_name_filter: str = ""
    name_search: str = ""
    official_definitions: Tuple[Tuple[str, int], ...] = ()


@dataclass(frozen=True)
class CalibrationSpec:
    """Per-repo join tables for Azure label mining (path buckets, blast radius, priors)."""

    tables: Mapping[str, Any]

    def table(self, key: str, default: Any = None) -> Any:
        return self.tables.get(key, default)


@dataclass(frozen=True)
class RuleSpec:
    """One invariant the brief states as a rule for the agent stage to test.

    `citation_kind` names which artifact the engine must cite for it. Every rule carries
    a citation into the tree, and a rule whose citation cannot be resolved is a bug in
    the adapter rather than a finding (HLD section 4.4).
    """

    id: str
    statement: str
    citation_kind: str
    derivation: str = "static"


@dataclass(frozen=True)
class RepoAdapter:
    """One supported repository, as a table of what Scout knows about it."""

    name: str
    summary: str
    # Alternative marker groups; the repo is identified when every path in any one group
    # is present. Groups rather than a flat list so detection survives an upstream rename.
    markers: Tuple[Tuple[str, ...], ...]
    path_classes: Tuple[PathClass, ...]
    path_rules: Tuple[PathRule, ...]
    fallback: PathClass
    entity_sources: Tuple[EntitySource, ...] = ()
    invariants: Tuple[InvariantSpec, ...] = ()
    ci_surfaces: Tuple[CiSurface, ...] = ()
    # The static stage's view of this repository. Absent means the repo is ingestible but
    # not yet analyzable, which is a legitimate state for a newly added adapter.
    entity_model: Optional[Union[DirectoryEntitySpec, FileEntitySpec]] = None
    coverage_spec: Optional[Union[PipelineCoverageSpec, ConstantCoverageSpec]] = None
    github_api: Optional[GitHubApiSpec] = None
    azure_devops: Optional[AzureDevOpsSpec] = None
    calibration: Optional[CalibrationSpec] = None
    rules: Tuple[RuleSpec, ...] = ()
    detectors: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name:
            raise RepoAdapterError("A RepoAdapter needs a name")
        if not self.path_classes:
            raise RepoAdapterError(f"Adapter {self.name!r} declares no path classes")
        if not self.markers or not all(self.markers):
            raise RepoAdapterError(f"Adapter {self.name!r} needs at least one non-empty marker group")

        ids = [path_class.id for path_class in self.path_classes]
        if len(set(ids)) != len(ids):
            raise RepoAdapterError(f"Adapter {self.name!r} repeats a path class id: {sorted(ids)}")

        foreign = sorted(item.qualified_id for item in self.path_classes if item.repo != self.name)
        if foreign:
            raise RepoAdapterError(f"Adapter {self.name!r} claims path classes owned by another repo: {foreign}")

        ranks = sorted(path_class.rank for path_class in self.path_classes)
        if ranks != list(range(1, len(ranks) + 1)):
            raise RepoAdapterError(
                f"Adapter {self.name!r} must rank its {len(ranks)} path classes 1..{len(ranks)}, got {ranks}"
            )

        known = set(self.path_classes)
        unknown = sorted(str(path_class) for _, path_class in self.path_rules if path_class not in known)
        if unknown:
            raise RepoAdapterError(f"Adapter {self.name!r} has rules for undeclared path classes: {unknown}")
        if self.fallback not in known:
            raise RepoAdapterError(f"Adapter {self.name!r} has a fallback it does not declare: {self.fallback}")
        if self.fallback.kind is not PathKind.OTHER:
            raise RepoAdapterError(f"Adapter {self.name!r} fallback {self.fallback} must be kind {PathKind.OTHER}")

    def classify(self, path: str) -> PathClass:
        """Classify a repo-relative path into this repository's blast-radius classes."""
        normalized = (path or "").strip()
        if normalized.startswith("./"):
            normalized = normalized[2:]
        for pattern, path_class in self.path_rules:
            if fnmatchcase(normalized, pattern):
                return path_class
        return self.fallback

    def path_class(self, class_id: str) -> PathClass:
        """Look a path class up by its adapter-scoped id, as `from_dict` does."""
        for path_class in self.path_classes:
            if path_class.id == class_id:
                return path_class
        raise RepoAdapterError(
            f"Adapter {self.name!r} has no path class {class_id!r}; it has {sorted(c.id for c in self.path_classes)}"
        )

    def ranked_classes(self) -> Tuple[PathClass, ...]:
        """This repository's classes, widest blast radius first."""
        return tuple(sorted(self.path_classes, key=lambda path_class: path_class.rank))

    def matches(self, exists: Callable[[str], bool]) -> bool:
        """Is a tree with these paths this repository? `exists` answers for one path."""
        return any(all(exists(marker) for marker in group) for group in self.markers)

    def entity_source(self, kind: str) -> Optional[EntitySource]:
        return next((source for source in self.entity_sources if source.kind == kind), None)
