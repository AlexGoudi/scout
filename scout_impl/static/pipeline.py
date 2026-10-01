"""The PR-CI coverage model, parsed out of the pipeline definition.

**This is the detector's source of ground truth and its failure mode is silent.** Every
finding Scout emits is a statement about what the pipeline builds, so a sloppy parse
changes every finding while the citations still resolve, the brief still validates and
the report still renders. Nothing downstream objects.

Four properties follow from that failure mode, each implemented here rather than described:

**Template indirection is resolved, not grepped for.** The job groups are not in the
pipeline file's job list. They are a parameter passed to
`.azure-pipelines/azure-pipelines-build.yml`, which forwards them to
`azure-pipelines-image-template.yml`, which forwards them to
`azure-pipelines-job-groups.yml`, which finally expands
`${{ each jobGroup in parameters.jobGroups }}` into one job apiece. `_resolve_jobs` walks
that chain with the caller's parameter bindings in hand. It has to: the middle template
also carries a **default** `jobGroups` list, holding the far larger official-build set,
which is selected by `${{ if eq(parameters.jobGroups, '') }}` and which a parser that
read the template without its bindings would report as PR coverage.

**The stage scope is explicit and missing stages are fatal.** `PipelineStageNotFound` is
raised rather than falling back to something that happens to return a plausible number.

**Two independent parses are cross-checked.** `loose_scan` is a line-oriented regex over
the raw bytes that knows nothing about stages, templates or YAML; `strict_parse` is the
resolver above. `parse_coverage_model` runs both and `CoverageModel.loose_scan_agrees`
records whether they matched. Disagreement is a test failure, not a warning.

**The result is published.** The job-group list, the scope, the templates that were read
and the conditions that could not be evaluated all reach the brief, so a reviewer can see
what Scout believed the pipeline does instead of taking the number on trust.

Known limitation, recorded rather than silently unhandled: matrix strategies and
conditional job inclusion beyond the `eq`/`ne`-against-empty forms below are not
evaluated. An unevaluated condition excludes its branch and is reported in
`CoverageModel.limitations`, because inventing a job group inflates coverage in the
direction that flatters Scout.
"""

import posixpath
import re
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

from ..repos.base import PipelineCoverageSpec
from .treeindex import TreeIndex


class PipelineParseError(RuntimeError):
    """The pipeline definition could not be read as a coverage model."""


class PipelineStageNotFound(PipelineParseError):
    """A stage the adapter names as in scope is not in the pipeline. Never a fallback."""


class PipelineParseDisagreement(PipelineParseError):
    """The strict parse and the loose scan disagree, so the coverage model cannot be trusted."""


_TEMPLATE_REF = re.compile(r"^(?P<path>[^@]+)(?:@(?P<repo>.+))?$")
_EACH_KEY = re.compile(r"^\$\{\{\s*each\s+(?P<var>\w+)\s+in\s+parameters\.(?P<param>\w+)\s*\}\}$")
_IF_KEY = re.compile(r"^\$\{\{\s*if\s+(?P<condition>.+?)\s*\}\}$")
_PARAM_REF = re.compile(r"^\$\{\{\s*parameters\.(?P<name>\w+)\s*\}\}$")
_EMPTY_COMPARE = re.compile(r"^(?P<op>eq|ne)\(\s*parameters\.(?P<name>\w+)\s*,\s*''\s*\)$")
_BARE_PARAM = re.compile(r"^parameters\.(?P<name>\w+)$")


@dataclass(frozen=True)
class JobGroup:
    """One job group the pipeline schedules, and what the pipeline says it builds."""

    name: str
    stage: str
    template: str
    family: str
    arch: str
    qualifier: str = ""
    pool: str = ""
    continue_on_error: bool = False
    line: int = 0
    line_end: int = 0

    @property
    def is_arch_qualified(self) -> bool:
        """Does this group build its family only for one architecture?

        Where the ambiguity starts. `marvell-prestera-arm64` builds family
        `marvell-prestera` **on arm64**, so a platform declaring the unqualified name
        matches only if its own architecture agrees, and `platform_asic` does not state
        one. A group whose name differs from its family but that declares no architecture
        of its own is not qualified at all — it simply has two names for the same thing,
        and both count as built.
        """
        return self.family != self.name and bool(self.qualifier)


@dataclass(frozen=True)
class CoverageModel:
    """What PR CI builds, plus the audit trail that says how Scout decided."""

    model: str
    path: str
    scope: Tuple[str, ...]
    job_groups: Tuple[JobGroup, ...]
    loose_names: Tuple[str, ...]
    templates_read: Tuple[str, ...]
    limitations: Tuple[str, ...] = ()
    stage_lines: Dict[str, int] = field(default_factory=dict)
    strict: bool = True
    # Every file the parse read, kept so a citation into one can carry its quote without
    # reading it again; a second read would move the counts NFR-12 reports.
    texts: Dict[str, str] = field(default_factory=dict)
    # Where the pipeline states the architecture a group builds when it names none, and
    # where the build is configured for it, as (path, first line, last line).
    arch_contract: Tuple[Tuple[str, int, int], ...] = ()

    @property
    def names(self) -> Tuple[str, ...]:
        return tuple(sorted({group.name for group in self.job_groups}))

    @property
    def built_families(self) -> Tuple[str, ...]:
        """Families some job group certainly builds, which is what C5 tests.

        A group's own name always counts. Its declared family counts too unless the group
        is architecture-qualified, in which case whether it covers a given entity is the
        undecidable part and belongs in `alias_families` instead.
        """
        names = {group.name for group in self.job_groups}
        names |= {group.family for group in self.job_groups if not group.is_arch_qualified}
        return tuple(sorted(names))

    @property
    def alias_families(self) -> Dict[str, Tuple[str, ...]]:
        """Unqualified family to the architecture-qualified groups that might build it."""
        aliases: Dict[str, List[str]] = {}
        for group in self.job_groups:
            if group.is_arch_qualified:
                aliases.setdefault(group.family, []).append(group.name)
        return {family: tuple(sorted(names)) for family, names in sorted(aliases.items())}

    @property
    def loose_scan_agrees(self) -> bool:
        return self.names == tuple(sorted(set(self.loose_names)))

    def group(self, name: str) -> Optional[JobGroup]:
        return next((item for item in self.job_groups if item.name == name), None)

    def quote(self, path: str, start: int, end: int) -> str:
        """Lines `start..end` of a file the parse read, or '' for one it did not."""
        text = self.texts.get(path)
        if text is None or start < 1 or end < start:
            return ""
        return "\n".join(text.splitlines()[start - 1:end])

    def as_parse_record(self) -> Dict[str, Any]:
        """The `coverage.parse` block: the parse itself, not only its result."""
        return {
            "scope": "+".join(self.scope),
            "strict": self.strict,
            "loose_scan_agrees": self.loose_scan_agrees,
            "loose_scan_names": sorted(set(self.loose_names)),
            "templates_read": list(self.templates_read),
            "limitations": list(self.limitations),
        }


def parse_coverage_model(tree: TreeIndex, spec: PipelineCoverageSpec) -> CoverageModel:
    """Parse the pipeline strictly, cross-check it loosely, and refuse to guess."""
    if not tree.exists(spec.path):
        raise PipelineParseError(f"{spec.path} is not in the tree at {tree.rev[:9]}")

    text = tree.read(spec.path)
    model = strict_parse(tree, spec, text)
    if not model.loose_scan_agrees:
        raise PipelineParseDisagreement(
            f"Two parses of {spec.path} disagree, which is how a wrong coverage model reaches every "
            f"finding without failing anything. Strict scope "
            f"{'+'.join(spec.stages)} found {len(model.names)}: {list(model.names)}. A loose "
            f"whole-file scan found {len(set(model.loose_names))}: {sorted(set(model.loose_names))}. "
            f"Resolve which is right before trusting any coverage number from this tree."
        )
    return model


def strict_parse(tree: TreeIndex, spec: PipelineCoverageSpec, text: Optional[str] = None) -> CoverageModel:
    """Resolve the named stages to the job groups they actually schedule."""
    text = tree.read(spec.path) if text is None else text
    document = _load_yaml(text, spec.path)

    stages = document.get("stages")
    if not isinstance(stages, list):
        raise PipelineParseError(f"{spec.path} declares no `stages` list, so it is not a pipeline Scout can scope")

    by_name = {}
    for stage in stages:
        if isinstance(stage, dict) and stage.get("stage"):
            by_name[str(stage["stage"])] = stage

    missing = [name for name in spec.stages if name not in by_name]
    if missing:
        raise PipelineStageNotFound(
            f"{spec.path} has no stage named {missing}; it declares {sorted(by_name)}. Scout will not fall "
            f"back to a whole-file scan, because a scan that happens to return something is how a wrong "
            f"coverage model ships silently."
        )

    resolver = _Resolver(tree, spec)
    groups: List[JobGroup] = []
    for name in spec.stages:
        groups.extend(resolver.stage_job_groups(name, by_name[name]))

    spans = item_spans(text.splitlines())
    for position, group in enumerate(groups):
        start, end = spans.get(group.name, (0, 0))
        groups[position] = replace(group, line=start, line_end=end)
    texts = {spec.path: text, **resolver.texts}

    return CoverageModel(
        model=spec.model,
        path=spec.path,
        scope=tuple(spec.stages),
        job_groups=tuple(groups),
        loose_names=loose_scan(text, spec.group_parameter),
        templates_read=tuple(resolver.templates_read),
        limitations=tuple(resolver.limitations),
        stage_lines=_stage_lines(text, spec.stages),
        texts=texts,
        arch_contract=_arch_contract(texts, [spec.path, *resolver.templates_read], spec),
    )


def loose_scan(text: str, group_parameter: str = "jobGroups") -> Tuple[str, ...]:
    """The deliberately naive second opinion: every job-group name anywhere in the file.

    Line-oriented and regex-based on purpose. It shares no code with the strict parse and
    does not even use a YAML library, so the two can only agree by both being right about
    the same file — which is the whole value of the cross-check.
    """
    header = re.compile(r"^(\s*)" + re.escape(group_parameter) + r":\s*(?:#.*)?$")
    item = re.compile(r"^(\s*)-\s+name:\s*['\"]?([A-Za-z0-9][\w.-]*)['\"]?\s*$")

    names: List[str] = []
    indent: Optional[int] = None
    for line in text.splitlines():
        if not line.strip():
            continue
        matched_header = header.match(line)
        if matched_header:
            indent = len(matched_header.group(1))
            continue
        if indent is None:
            continue

        matched_item = item.match(line)
        if matched_item and len(matched_item.group(1)) >= indent:
            names.append(matched_item.group(2))
            continue
        if len(line) - len(line.lstrip()) <= indent and not matched_item:
            indent = None
    return tuple(names)


class _Resolver:
    """Walks one stage's jobs through the template chain, carrying parameter bindings."""

    def __init__(self, tree: TreeIndex, spec: PipelineCoverageSpec) -> None:
        self.tree = tree
        self.spec = spec
        self.templates_read: List[str] = []
        self.limitations: List[str] = []
        self.texts: Dict[str, str] = {}

    def stage_job_groups(self, stage_name: str, stage: Dict[str, Any]) -> List[JobGroup]:
        jobs = stage.get("jobs")
        if not isinstance(jobs, list):
            raise PipelineParseError(f"Stage {stage_name!r} in {self.spec.path} declares no `jobs` list")
        return self._jobs(jobs, {}, self.spec.path, stage_name, depth=0)

    def _jobs(
        self,
        jobs: Sequence[Any],
        params: Dict[str, Any],
        source_path: str,
        stage_name: str,
        depth: int,
    ) -> List[JobGroup]:
        if depth > self.spec.max_template_depth:
            raise PipelineParseError(
                f"Template nesting in {self.spec.path} exceeded {self.spec.max_template_depth} levels at "
                f"{source_path}; this is a loop or a pipeline shape Scout does not model"
            )

        found: List[JobGroup] = []
        for entry in jobs:
            if not isinstance(entry, dict):
                continue
            for node in self._flatten(entry, params):
                found.extend(self._job(node, params, source_path, stage_name, depth))
        return found

    def _job(
        self,
        node: Dict[str, Any],
        params: Dict[str, Any],
        source_path: str,
        stage_name: str,
        depth: int,
    ) -> List[JobGroup]:
        expansion = self._each_expansion(node, params)
        if expansion is not None:
            return [self._job_group(item, stage_name, source_path) for item in expansion]

        template = node.get("template")
        if not template:
            return []

        target = self._template_path(str(template), source_path)
        if target is None:
            return []

        document = _load_yaml(self._read_template(target), target)
        bound = self._bind(document, node.get("parameters"), params)
        nested = document.get("jobs")
        if not isinstance(nested, list):
            return []
        return self._jobs(nested, bound, target, stage_name, depth + 1)

    def _each_expansion(self, node: Dict[str, Any], params: Dict[str, Any]) -> Optional[List[Any]]:
        """`- ${{ each jobGroup in parameters.jobGroups }}:` — the end of the chain."""
        if len(node) != 1:
            return None
        key = next(iter(node))
        matched = _EACH_KEY.match(str(key))
        if not matched or matched.group("param") != self.spec.group_parameter:
            return None
        bound = params.get(self.spec.group_parameter)
        return [item for item in bound if isinstance(item, dict)] if isinstance(bound, list) else []

    def _bind(self, document: Dict[str, Any], passed: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        """Template defaults overlaid with what the caller actually passed."""
        bound: Dict[str, Any] = {}
        for declared in document.get("parameters") or []:
            if isinstance(declared, dict) and declared.get("name"):
                bound[str(declared["name"])] = declared.get("default", "")
        if isinstance(passed, dict):
            for key, value in self._flatten_mapping(passed, params).items():
                bound[key] = _substitute(value, params)
        return bound

    def _flatten(self, node: Dict[str, Any], params: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Expand `${{ if ... }}` keys in a jobs-list entry into zero or more real entries."""
        if len(node) == 1:
            key = str(next(iter(node)))
            matched = _IF_KEY.match(key)
            if matched:
                if not self._condition(matched.group("condition"), params):
                    return []
                value = node[next(iter(node))]
                if isinstance(value, dict):
                    return [value]
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
                return []
        return [node]

    def _flatten_mapping(self, mapping: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        """The same expansion inside a `parameters:` mapping, which is where it matters.

        `${{ if ne(parameters.jobGroups, '') }}: {jobGroups: ...}` against
        `${{ if eq(parameters.jobGroups, '') }}: {jobGroups: <official build default>}` is
        the branch that decides whether Scout reports PR coverage or post-merge coverage.
        """
        flattened: Dict[str, Any] = {}
        for key, value in mapping.items():
            matched = _IF_KEY.match(str(key))
            if matched:
                if self._condition(matched.group("condition"), params) and isinstance(value, dict):
                    flattened.update(self._flatten_mapping(value, params))
                continue
            flattened[str(key)] = value
        return flattened

    def _condition(self, condition: str, params: Dict[str, Any]) -> bool:
        compared = _EMPTY_COMPARE.match(condition)
        if compared:
            empty = _is_empty(params.get(compared.group("name"), ""))
            return empty if compared.group("op") == "eq" else not empty

        bare = _BARE_PARAM.match(condition)
        if bare:
            return bool(params.get(bare.group("name")))

        note = f"condition not evaluated, branch excluded: ${{{{ if {condition} }}}}"
        if note not in self.limitations:
            self.limitations.append(note)
        return False

    def _template_path(self, reference: str, source_path: str) -> Optional[str]:
        """Azure resolves a bare template against the including file; `@repo` against a root."""
        matched = _TEMPLATE_REF.match(reference.strip())
        if not matched:
            return None

        path = matched.group("path").strip()
        repo = matched.group("repo")
        if repo and repo not in ("self", "buildimage"):
            note = f"template in another repository, not followed: {reference}"
            if note not in self.limitations:
                self.limitations.append(note)
            return None

        resolved = path if repo else posixpath.normpath(posixpath.join(posixpath.dirname(source_path), path))
        if not self.tree.exists(resolved):
            note = f"template not in the tree, not followed: {reference}"
            if note not in self.limitations:
                self.limitations.append(note)
            return None
        return resolved

    def _read_template(self, path: str) -> str:
        if len(self.templates_read) >= self.spec.max_templates:
            raise PipelineParseError(
                f"Resolving {self.spec.path} wanted more than {self.spec.max_templates} templates; "
                f"read so far: {self.templates_read}"
            )
        if path not in self.templates_read:
            self.templates_read.append(path)
        # Read through the tree every time, as before: the blob cache counts the repeats.
        text = self.tree.read(path)
        self.texts.setdefault(path, text)
        return text

    def _job_group(self, item: Dict[str, Any], stage_name: str, source_path: str) -> JobGroup:
        name = str(item.get("name") or "").strip()
        if not name:
            raise PipelineParseError(f"A job group in stage {stage_name!r} of {self.spec.path} has no name")

        variables = item.get("variables") if isinstance(item.get("variables"), dict) else {}
        family = str(variables.get(self.spec.family_variable) or name).strip() if self.spec.family_variable else name
        declared_arch = str(variables.get(self.spec.arch_variable) or "").strip() if self.spec.arch_variable else ""
        return JobGroup(
            name=name,
            stage=stage_name,
            template=source_path,
            family=family or name,
            arch=declared_arch or self.spec.default_arch,
            # Only an architecture the group states for itself qualifies it. Inheriting the
            # pipeline's default would make every group look qualified and put the whole
            # tree in the ambiguous set.
            qualifier=declared_arch,
            pool=str(item.get("pool") or ""),
            continue_on_error=bool(item.get("continueOnError")),
        )


def _load_yaml(text: str, path: str) -> Dict[str, Any]:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise PipelineParseError(f"{path} is not loadable YAML: {error}") from error
    if not isinstance(document, dict):
        raise PipelineParseError(f"{path} does not parse to a mapping, so it is not a pipeline definition")
    return document


def _substitute(value: Any, params: Dict[str, Any]) -> Any:
    """Replace a whole-value `${{ parameters.x }}` passthrough with what x is bound to."""
    if isinstance(value, str):
        matched = _PARAM_REF.match(value.strip())
        if matched:
            return params.get(matched.group("name"), "")
    return value


def _is_empty(value: Any) -> bool:
    return value == "" or value is None or value == []


def _stage_lines(text: str, stages: Sequence[str]) -> Dict[str, int]:
    header = re.compile(r"^\s*-\s+stage:\s*['\"]?([A-Za-z0-9_][\w.-]*)['\"]?\s*$")
    wanted = set(stages)
    lines: Dict[str, int] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        matched = header.match(line)
        if matched and matched.group(1) in wanted:
            lines.setdefault(matched.group(1), number)
    return lines


_ITEM = re.compile(r"^(\s*)-\s+name:\s*['\"]?([A-Za-z0-9][\w.-]*)['\"]?\s*$")


def item_spans(rows: Sequence[str]) -> Dict[str, Tuple[int, int]]:
    """First and last line of each job-group item, first declaration of a name winning.

    An item runs from its `- name:` line through every following line indented deeper
    than its dash, which is what YAML makes it; trailing blank lines are not part of it.
    """
    spans: Dict[str, Tuple[int, int]] = {}
    for index, line in enumerate(rows):
        matched = _ITEM.match(line)
        if not matched or matched.group(2) in spans:
            continue
        indent = len(matched.group(1))
        last = index
        for following in range(index + 1, len(rows)):
            body = rows[following]
            if not body.strip():
                continue
            if len(body) - len(body.lstrip()) <= indent:
                break
            last = following
        spans[matched.group(2)] = (index + 1, last + 1)
    return spans


def _arch_contract(texts: Dict[str, str], paths: Sequence[str],
                   spec: PipelineCoverageSpec) -> Tuple[Tuple[str, int, int], ...]:
    """Where the default architecture is set, and where the build is configured for it.

    Found by the adapter's own variable names rather than by line number, in the files the
    parse already read: the first `<arch>: <default>` assignment, and the first line
    passing `<arch>=$(<arch>)` to the build, widened by one line when the line above it
    assigns the family variable, since that pair is the statement "a group builds its
    family for its architecture".
    """
    if not spec.arch_variable:
        return ()
    arch = re.escape(spec.arch_variable)
    default = (re.compile(rf"^\s*{arch}:\s*['\"]?{re.escape(spec.default_arch)}['\"]?\s*$")
               if spec.default_arch else None)
    configure = re.compile(rf"{arch}=\$\({arch}\)")
    family = re.compile(rf"\b{re.escape(spec.family_variable)}=") if spec.family_variable else None

    found: List[Tuple[str, int, int]] = []
    seen_default = default is None
    seen_configure = False
    for path in paths:
        rows = texts.get(path, "").splitlines()
        for number, line in enumerate(rows, start=1):
            if not seen_default and default.match(line):
                found.append((path, number, number))
                seen_default = True
            if not seen_configure and configure.search(line):
                above = number > 1 and family is not None and family.search(rows[number - 2])
                found.append((path, number - 1 if above else number, number))
                seen_configure = True
    return tuple(found)
