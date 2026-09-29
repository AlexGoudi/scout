"""Rule-based change type, with the evidence for every type that matched.

The primary type is the first match in ``PRECEDENCE``; every match is listed so a model or
an LLM can see that, say, a platform addition also fixed something.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from ..mining.taxonomy import DEFAULT_SZZ_CLASSES

PRECEDENCE = (
    "revert",
    "submodule-bump",
    "docs",
    "test",
    "dependency-bump",
    "platform-support",
    "fix",
    "build-ci",
    "feature",
    "chore",
)

FIX_SUBJECT_RE = re.compile(
    r"\b(fix(e[sd]|ing)?|bug(s|fix|fixes)?|hotfix|crash(es|ed)?|leak(s|ed)?|regression|broken|incorrect|wrong|"
    r"hang(s|ing)?|race|deadlock|segfault|panic|core ?dump|workaround|cve-\d+|vulnerabilit(y|ies)|typo)\b",
    re.IGNORECASE,
)
FIX_BODY_RE = re.compile(
    r"^\s*(fix(es|ed)?|close[sd]?|resolve[sd]?)\s*:?\s*(#\d+|https://github\.com/\S+/issues/\d+)",
    re.IGNORECASE | re.MULTILINE,
)
DEPENDENCY_SUBJECT_RE = re.compile(
    r"\b(bump|upgrade|update|uprev)s?\b.*?(\bv?\d+(\.\d+)+\b|\bversions?\b|\bfrom\b.*\bto\b)", re.IGNORECASE
)
PLATFORM_SUBJECT_RE = re.compile(
    r"\b(add|adding|added|support|introduce|new|onboard)\b.*?\b(platform|hwsku|sku|board|asic|chassis|linecard)s?\b",
    re.IGNORECASE,
)
FEATURE_SUBJECT_RE = re.compile(
    r"^(\[[^\]]*\]\s*|[\w./-]+:\s*)*(add|adding|added|support|introduce|implement|enable|new)\b|\bfeature\b",
    re.IGNORECASE,
)
TEST_TAGS = frozenset({"test", "tests", "unit test", "unittest", "pytest", "ut"})
BUILD_CI_TAGS = frozenset({"ci", "build", "azp", "pipeline", "pipelines", "github", "actions", "makefile"})
DEPENDENCY_PATHS_RE = re.compile(r"(^|/)(files/build/versions/|requirements[^/]*\.txt$|go\.(mod|sum)$)")
CI_PATHS_RE = re.compile(r"^(\.github/|\.azure-pipelines/|azure-pipelines[^/]*\.yml$|jenkins/|Jenkinsfile$)")
SZZ_CLASSES = DEFAULT_SZZ_CLASSES


@dataclass(frozen=True)
class Category:
    change_type: str
    change_types: tuple[str, ...]
    evidence: tuple[str, ...]

    @property
    def fix_like(self) -> bool:
        return "fix" in self.change_types and self.change_type != "revert"


def categorize(record: Mapping[str, Any]) -> Category:
    """Categorize one commit record (the dict form of ``scout-commit`` 1.0)."""
    message = record["message"]
    features = record["features"]
    files = record["files"]
    subject = message["subject"]
    tags = set(message["subject_tags"])
    total = len(files)
    paths = [item["new_path"] or item["old_path"] for item in files]
    evidence: dict[str, list[str]] = {}

    def hit(kind: str, reason: str) -> None:
        evidence.setdefault(kind, []).append(reason)

    if message["revert"]["is_revert"]:
        hit("revert", "subject is a revert")

    moved = [item["new_path"] for item in files if item["file_class"] == "submodule" and item["status"] == "M"]
    others = [item for item in files if item["file_class"] != "submodule"]
    if moved and all(item["file_class"] in ("build", "config", "doc", "other") for item in others):
        hit("submodule-bump", f"gitlinks moved: {', '.join(sorted(moved)[:3])}")

    if total and features["is_doc_only"]:
        hit("docs", f"all {total} files are documentation")
    if total and features["is_test_only"]:
        hit("test", f"all {total} files are tests")
    elif tags & TEST_TAGS:
        hit("test", f"subject tag {sorted(tags & TEST_TAGS)[0]!r}")

    dependency = DEPENDENCY_SUBJECT_RE.search(subject)
    if "dependabot" in subject.lower():
        hit("dependency-bump", "automated dependency update")
    elif dependency and not moved:
        hit("dependency-bump", f"subject: {dependency.group(0)[:40]!r}")
    elif total and all(DEPENDENCY_PATHS_RE.search(path) for path in paths):
        hit("dependency-bump", "only version pins changed")

    device_added = sum(1 for item in files if item["status"] == "A" and (item["new_path"] or "").startswith("device/"))
    hardware = sum(1 for path in paths if path.startswith(("device/", "platform/")))
    if device_added and hardware * 2 >= total:
        hit("platform-support", f"{device_added} files added under device/")
    elif PLATFORM_SUBJECT_RE.search(subject):
        hit("platform-support", f"subject: {PLATFORM_SUBJECT_RE.search(subject).group(0)[:40]!r}")

    fix_subject = FIX_SUBJECT_RE.search(subject)
    if fix_subject:
        hit("fix", f"subject keyword {fix_subject.group(0).lower()!r}")
    elif FIX_BODY_RE.search(message["body"]):
        hit("fix", "body references a fixed issue")

    if total and all(CI_PATHS_RE.search(path) or item["file_class"] == "build" for path, item in zip(paths, files)):
        hit("build-ci", "only build or CI files changed")
    elif tags & BUILD_CI_TAGS:
        hit("build-ci", f"subject tag {sorted(tags & BUILD_CI_TAGS)[0]!r}")

    feature = FEATURE_SUBJECT_RE.search(subject)
    if feature:
        hit("feature", f"subject: {feature.group(0).strip()[:40]!r}")

    if not evidence:
        hit("chore", "no rule matched")
    types = tuple(kind for kind in PRECEDENCE if kind in evidence)
    return Category(
        change_type=types[0],
        change_types=types,
        evidence=tuple(f"{kind}: {reason}" for kind in types for reason in evidence[kind]),
    )
