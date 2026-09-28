"""Registry of the repositories Scout can analyze, and how a caller picks one.

Two ways in. A caller that knows the repository names it — `resolve_adapter("sonic-
buildimage")` — and a caller that does not lets Scout identify it from the tree, either
from a checkout on disk or from a listing of the paths at a commit. The listing form is
what makes detection work against a remote that was never cloned, because a blob-filtered
fetch already holds every tree it needs.

Adapters register themselves here at import time, the same way a provider does in
`provider.py`. Adding a repository is one module plus one `register_adapter` call.
"""

from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set

from . import sonic_buildimage, sonic_mgmt
from .base import (
    CiSurface,
    DirectoryEntitySpec,
    EntitySource,
    InvariantSpec,
    PathClass,
    PathKind,
    PathRule,
    PipelineCoverageSpec,
    RepoAdapter,
    RepoAdapterError,
    RuleSpec,
)

_ADAPTERS: Dict[str, RepoAdapter] = {}


def register_adapter(adapter: RepoAdapter) -> None:
    """Register an adapter under its name, replacing any adapter already registered."""
    _ADAPTERS[adapter.name] = adapter


def available_adapters() -> List[str]:
    return sorted(_ADAPTERS)


def get_adapter(name: str) -> RepoAdapter:
    adapter = _ADAPTERS.get(name)
    if adapter is None:
        raise RepoAdapterError(f"Unknown repository {name!r}; available: {available_adapters()}")
    return adapter


def detect_adapter(exists: Callable[[str], bool]) -> Optional[RepoAdapter]:
    """Identify the repository from a path-existence predicate, or return None.

    Ambiguity is an error rather than a coin toss: two adapters both claiming a tree
    means their markers are wrong, and silently picking one hides that.
    """
    matched = [adapter for adapter in _ADAPTERS.values() if adapter.matches(exists)]
    if len(matched) > 1:
        raise RepoAdapterError(
            f"Tree matches more than one repository: {sorted(adapter.name for adapter in matched)}"
        )
    return matched[0] if matched else None


def detect_in_checkout(root: Path) -> Optional[RepoAdapter]:
    """Identify the repository from a working copy on disk."""
    root = Path(root)
    return detect_adapter(lambda marker: (root / marker).exists())


def detect_in_tree(paths: Iterable[str]) -> Optional[RepoAdapter]:
    """Identify the repository from a listing of the paths at one commit."""
    return detect_adapter(_path_universe(paths).__contains__)


def resolve_adapter(
    name: Optional[str] = None,
    root: Optional[Path] = None,
    paths: Optional[Iterable[str]] = None,
) -> RepoAdapter:
    """Name a repository, or let Scout identify one, and get its adapter or an error."""
    if name:
        return get_adapter(name)

    if root is not None:
        detected = detect_in_checkout(root)
        source = f"the checkout at {Path(root)}"
    elif paths is not None:
        detected = detect_in_tree(paths)
        source = "the fetched tree"
    else:
        raise RepoAdapterError("resolve_adapter needs a repository name, a checkout root or a tree listing")

    if detected is None:
        raise RepoAdapterError(
            f"Could not tell which repository {source} is; name one of {available_adapters()} explicitly"
        )
    return detected


def path_class_from_qualified_id(value: str) -> PathClass:
    """Read back a `repo:id` path class, the form a serialized change set carries."""
    repo, separator, class_id = str(value).partition(":")
    if not separator:
        raise RepoAdapterError(f"Path class {value!r} is not qualified; expected REPO:CLASS_ID")
    return get_adapter(repo).path_class(class_id)


def _path_universe(paths: Iterable[str]) -> Set[str]:
    """The paths plus every directory above them, so a directory marker resolves too.

    A recursive tree listing names blobs only, while a marker may legitimately be a
    directory; expanding the parents here keeps markers written the same way whether
    they are checked against a filesystem or against a listing.
    """
    universe: Set[str] = set()
    for path in paths:
        parts = str(path).strip("/").split("/")
        for depth in range(1, len(parts) + 1):
            universe.add("/".join(parts[:depth]))
    return universe


register_adapter(sonic_mgmt.ADAPTER)
register_adapter(sonic_buildimage.ADAPTER)

__all__ = [
    "CiSurface",
    "DirectoryEntitySpec",
    "EntitySource",
    "InvariantSpec",
    "PathClass",
    "PathKind",
    "PathRule",
    "PipelineCoverageSpec",
    "RepoAdapter",
    "RepoAdapterError",
    "RuleSpec",
    "available_adapters",
    "detect_adapter",
    "detect_in_checkout",
    "detect_in_tree",
    "get_adapter",
    "path_class_from_qualified_id",
    "register_adapter",
    "resolve_adapter",
]
