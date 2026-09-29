"""Path taxonomy: components, feature areas, entities and file classes, from paths alone."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from ..static.related import FeatureMap, FeatureMapError, load_feature_map

SCOUT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TAXONOMY = SCOUT_ROOT / "scout_impl" / "repos" / "buildimage" / "taxonomy.yaml"
FILE_CLASSES = ("code", "test", "doc", "config", "build", "yang", "patch", "binary", "submodule", "other")
DEFAULT_SZZ_CLASSES = frozenset({"code", "config", "build", "yang", "patch"})
ENTITY_KINDS = ("vendor", "platform", "hwsku", "asic", "docker", "submodule")
DOCKER_RULE_RE = re.compile(r"docker-(?P<name>.+)\.(?:mk|dep)")


class TaxonomyError(ValueError):
    """Raised when the taxonomy file or the feature map it names is invalid."""


@dataclass(frozen=True)
class Rule:
    id: str
    patterns: tuple[str, ...]
    regex: re.Pattern[str]

    def matches(self, path: str) -> bool:
        return self.regex.fullmatch(path) is not None


@dataclass(frozen=True)
class FeatureArea:
    id: str
    prefixes: tuple[str, ...]

    def matches(self, path: str) -> bool:
        return any(path == prefix or path.startswith(prefix + "/") for prefix in self.prefixes)


@dataclass(frozen=True)
class Taxonomy:
    sha256: str
    bots: tuple[str, ...]
    name_stoplist: tuple[str, ...]
    components: tuple[Rule, ...]
    features: tuple[FeatureArea, ...]
    file_classes: tuple[Rule, ...]
    device_vendor_exclude: frozenset[str]
    hwsku_exclude: frozenset[str]
    asic_exclude: frozenset[str]
    szz_file_classes: frozenset[str] = DEFAULT_SZZ_CLASSES


@dataclass(frozen=True)
class AreaCount:
    id: str
    path_count: int


@dataclass(frozen=True)
class Entity:
    id: str
    kind: str
    name: str
    path_count: int


@dataclass(frozen=True)
class Areas:
    components: tuple[AreaCount, ...]
    features: tuple[AreaCount, ...]
    entities: tuple[Entity, ...]
    unmapped_paths: tuple[str, ...]


def glob_to_regex(pattern: str) -> str:
    """Translate one taxonomy pattern; see the header of taxonomy.yaml for the semantics."""
    anchored = "/" in pattern
    body = pattern.lstrip("/")
    pieces = []
    index = 0
    while index < len(body):
        if body.startswith("**/", index):
            pieces.append("(?:.*/)?")
            index += 3
        elif body.startswith("/**", index) and index + 3 == len(body):
            pieces.append("(?:/.*)?")
            index += 3
        elif body.startswith("**", index):
            pieces.append(".*")
            index += 2
        elif body[index] == "*":
            pieces.append("[^/]*")
            index += 1
        elif body[index] == "?":
            pieces.append("[^/]")
            index += 1
        else:
            pieces.append(re.escape(body[index]))
            index += 1
    core = "".join(pieces)
    return core if anchored else f"(?:.*/)?{core}"


@lru_cache(maxsize=8)
def load_taxonomy(path: str | Path | None = None) -> Taxonomy:
    taxonomy_path = Path(path) if path is not None else DEFAULT_TAXONOMY
    try:
        raw = taxonomy_path.read_bytes()
        data = yaml.safe_load(raw)
    except (OSError, yaml.YAMLError) as exc:
        raise TaxonomyError(f"cannot read taxonomy {taxonomy_path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise TaxonomyError(f"{taxonomy_path}: schema_version must be 1")

    feature_spec = _mapping(data, "features")
    try:
        feature_map = load_feature_map(SCOUT_ROOT / str(feature_spec.get("source", "")))
    except FeatureMapError as exc:
        raise TaxonomyError(str(exc)) from exc
    prefix = str(feature_spec.get("prefix", ""))

    entities = _mapping(data, "entities")
    szz_classes = frozenset(str(item) for item in data.get("szz_file_classes") or DEFAULT_SZZ_CLASSES)
    unknown = sorted(szz_classes - set(FILE_CLASSES))
    if unknown:
        raise TaxonomyError(f"{taxonomy_path}: szz_file_classes names unknown classes {unknown}")
    return Taxonomy(
        sha256=hashlib.sha256(raw + b"\x00" + feature_map.raw).hexdigest(),
        bots=tuple(str(item).lower() for item in _list(data, "bots")),
        name_stoplist=tuple(str(item) for item in _list(data, "name_stoplist")),
        components=_rules(_list(data, "components"), "id"),
        features=_features(feature_map, prefix),
        file_classes=_rules(_list(data, "file_classes"), "class", allowed=FILE_CLASSES),
        device_vendor_exclude=frozenset(str(item) for item in entities.get("device_vendor_exclude", ())),
        hwsku_exclude=frozenset(str(item) for item in entities.get("hwsku_exclude", ())),
        asic_exclude=frozenset(str(item) for item in entities.get("asic_exclude", ())),
        szz_file_classes=szz_classes,
    )


def classify(paths: Iterable[str], gitlinks: Mapping[str, str], taxonomy: Taxonomy) -> Areas:
    """Areas touched by ``paths``; ``gitlinks`` maps each gitlink path to its submodule name."""
    unique = sorted(set(paths))
    components: Counter[str] = Counter()
    features: Counter[str] = Counter()
    unmapped = []
    for path in unique:
        matched = [rule.id for rule in taxonomy.components if rule.matches(path)]
        components.update(matched)
        if not matched:
            unmapped.append(path)
        features.update(area.id for area in taxonomy.features if area.matches(path))
    return Areas(
        components=tuple(AreaCount(key, components[key]) for key in sorted(components)),
        features=tuple(AreaCount(key, features[key]) for key in sorted(features)),
        entities=_entities(unique, gitlinks, taxonomy),
        unmapped_paths=tuple(unmapped),
    )


def file_class(path: str, *, binary: bool, gitlink: bool, taxonomy: Taxonomy) -> str:
    if gitlink:
        return "submodule"
    if binary:
        return "binary"
    for rule in taxonomy.file_classes:
        if rule.matches(path):
            return rule.id
    return "other"


def _entities(paths: list[str], gitlinks: Mapping[str, str], taxonomy: Taxonomy) -> tuple[Entity, ...]:
    found: dict[tuple[str, str], set[str]] = {}

    def add(kind: str, name: str, path: str) -> None:
        if name:
            found.setdefault((kind, name), set()).add(path)

    for path in paths:
        parts = path.split("/")
        if parts[0] == "device" and len(parts) >= 3 and parts[1] not in taxonomy.device_vendor_exclude:
            add("vendor", parts[1], path)
            if len(parts) >= 4:
                add("platform", parts[2], path)
            if len(parts) >= 5 and parts[3] not in taxonomy.hwsku_exclude:
                add("hwsku", parts[3], path)
        elif parts[0] == "platform" and len(parts) >= 3 and parts[1] not in taxonomy.asic_exclude:
            add("asic", parts[1], path)
            if len(parts) >= 4 and parts[2].startswith("docker-"):
                add("docker", parts[2][len("docker-"):], path)
            elif len(parts) == 3 and DOCKER_RULE_RE.fullmatch(parts[2]):
                add("docker", DOCKER_RULE_RE.fullmatch(parts[2]).group("name"), path)
        elif parts[0] == "dockers" and len(parts) >= 3 and parts[1].startswith("docker-"):
            add("docker", parts[1][len("docker-"):], path)
        elif parts[0] == "rules" and len(parts) == 2 and DOCKER_RULE_RE.fullmatch(parts[1]):
            add("docker", DOCKER_RULE_RE.fullmatch(parts[1]).group("name"), path)
    for path, name in gitlinks.items():
        add("submodule", name, path)
    order = {kind: index for index, kind in enumerate(ENTITY_KINDS)}
    return tuple(
        Entity(id=f"{kind}:{name}", kind=kind, name=name, path_count=len(found[(kind, name)]))
        for kind, name in sorted(found, key=lambda item: (order[item[0]], item[1]))
    )


def _rules(items: list[Any], key: str, allowed: tuple[str, ...] | None = None) -> tuple[Rule, ...]:
    rules = []
    seen = set()
    for item in items:
        if not isinstance(item, dict) or not item.get(key) or not item.get("patterns"):
            raise TaxonomyError(f"every rule needs '{key}' and 'patterns': {item!r}")
        rule_id = str(item[key])
        if allowed is not None and rule_id not in allowed:
            raise TaxonomyError(f"unknown class {rule_id!r}; expected one of {allowed}")
        if rule_id in seen:
            raise TaxonomyError(f"duplicate rule {rule_id!r}")
        seen.add(rule_id)
        patterns = tuple(str(pattern) for pattern in item["patterns"])
        regex = re.compile("|".join(f"(?:{glob_to_regex(pattern)})" for pattern in patterns))
        rules.append(Rule(rule_id, patterns, regex))
    return tuple(rules)


def _features(feature_map: FeatureMap, prefix: str) -> tuple[FeatureArea, ...]:
    areas = []
    for area_id in sorted(feature_map.entries):
        prefixes = sorted(
            {entry[len(prefix):].strip("/") for entry in feature_map.entries[area_id] if entry.startswith(prefix)}
        )
        if prefixes:
            areas.append(FeatureArea(str(area_id), tuple(prefixes)))
    return tuple(areas)


def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key) or {}
    if not isinstance(value, dict):
        raise TaxonomyError(f"'{key}' must be a mapping")
    return value


def _list(data: dict[str, Any], key: str) -> list[Any]:
    value = data.get(key) or []
    if not isinstance(value, list):
        raise TaxonomyError(f"'{key}' must be a list")
    return value
