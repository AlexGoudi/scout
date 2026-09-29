"""Paths to assess: which feature groups a change touches, and what else those groups hold.

`datapath.json` names feature groups (bgp, lldp, acl, ...) and lists, for each, the
directories and files that implement or test it across repositories, every entry prefixed
with the repository it lives in (`sonic-buildimage/...`, `sonic-mgmt/...`). A changed path
belongs to a group when it equals one of the group's entries for its repository or lies
under one.

The query keeps the rule the original group lookup documented: the groups every matched
path shares, or, when they share none, every group any of them is in. A path in no group
contributes nothing rather than emptying the intersection, so one unrelated file (a README,
a pipeline YAML) does not widen a focused change into the union.

The loader is shared with `mining/taxonomy.py`, which hashes the same raw bytes into
`taxonomy_sha`; `FeatureMap.raw` is those bytes, unmodified.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Tuple, Union

FEATURE_MAP = Path(__file__).with_name("datapath.json")

RULE_SHARED = "shared"
RULE_UNION = "union"
RULE_NONE = "none"


class FeatureMapError(ValueError):
    """The feature map is missing, unparseable, or not an object of group -> paths."""


@dataclass(frozen=True)
class FeatureHit:
    """One selected group and the changed paths that fall in it."""

    id: str
    changed: Tuple[str, ...]

    def to_dict(self) -> Dict[str, object]:
        return {"id": self.id, "changed": list(self.changed)}


@dataclass(frozen=True)
class Related:
    """What `FeatureMap.related` found for one change."""

    repo: str
    rule: str
    features: Tuple[FeatureHit, ...]
    paths_to_assess: Tuple[Tuple[str, Tuple[str, ...]], ...]

    @property
    def empty(self) -> bool:
        return not self.features

    def to_dict(self) -> Dict[str, object]:
        return {
            "repo": self.repo,
            "rule": self.rule,
            "features": [hit.to_dict() for hit in self.features],
            "repos": [{"repo": repo, "paths": list(paths)} for repo, paths in self.paths_to_assess],
        }


@dataclass(frozen=True)
class FeatureMap:
    """The parsed map: each group's raw entries, and the bytes they were read from."""

    raw: bytes
    entries: Mapping[str, Tuple[str, ...]]

    @property
    def groups(self) -> Dict[str, Dict[str, Tuple[str, ...]]]:
        """group -> {repo: prefixes}, the prefixes relative to their repository."""
        grouped: Dict[str, Dict[str, List[str]]] = {}
        for group, entries in self.entries.items():
            by_repo = grouped.setdefault(group, {})
            for entry in entries:
                repo, _, prefix = entry.strip("/").partition("/")
                if prefix:
                    by_repo.setdefault(repo, []).append(prefix)
        return {group: {repo: tuple(sorted(set(prefixes))) for repo, prefixes in sorted(by_repo.items())}
                for group, by_repo in sorted(grouped.items())}

    def related(self, repo: str, changed_paths: Iterable[str]) -> Related:
        changed = sorted(set(changed_paths))
        groups = self.groups
        hits: Dict[str, List[str]] = {}
        memberships = []
        for path in changed:
            member_of = {group for group, by_repo in groups.items()
                         if any(_under(path, prefix) for prefix in by_repo.get(repo, ()))}
            if member_of:
                memberships.append(member_of)
                for group in member_of:
                    hits.setdefault(group, []).append(path)
        if not memberships:
            return Related(repo=repo, rule=RULE_NONE, features=(), paths_to_assess=())

        shared = set.intersection(*memberships)
        selected = sorted(shared or set.union(*memberships))
        assess: Dict[str, set] = {}
        for group in selected:
            for member_repo, prefixes in groups[group].items():
                for prefix in prefixes:
                    if member_repo == repo and any(_under(path, prefix) for path in changed):
                        continue
                    assess.setdefault(member_repo, set()).add(prefix)
        return Related(
            repo=repo,
            rule=RULE_SHARED if shared else RULE_UNION,
            features=tuple(FeatureHit(group, tuple(hits[group])) for group in selected),
            paths_to_assess=tuple((member_repo, tuple(sorted(paths))) for member_repo, paths in sorted(assess.items())),
        )


def load_feature_map(path: Optional[Union[str, Path]] = None) -> FeatureMap:
    source = Path(path) if path is not None else FEATURE_MAP
    try:
        raw = source.read_bytes()
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise FeatureMapError(f"cannot read feature map {source}: {exc}") from exc
    if not isinstance(data, dict) or not all(isinstance(entries, list) for entries in data.values()):
        raise FeatureMapError("feature map must be an object of area -> paths")
    return FeatureMap(raw=raw, entries={str(group): tuple(str(entry) for entry in entries)
                                        for group, entries in data.items()})


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")
