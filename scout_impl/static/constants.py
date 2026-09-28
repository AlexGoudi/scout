"""A coverage model read out of a Python constant, for repositories that declare one there.

The second coverage shape, added for the `sonic-mgmt` adapter and recorded as a core
change the second repository forced. `sonic-buildimage` says what PR CI builds in a
pipeline definition; `sonic-mgmt` says it in `PR_TOPOLOGY_TYPE`, a list literal in
`.azure-pipelines/impacted_area_testing/constant.py`. The two have nothing structurally in
common beyond producing a set of names, so this is a separate parser that returns the same
`CoverageModel` rather than a generalisation of the pipeline one.

Parsed with `ast`, never executed. The file is another repository's source, Scout is a
read-only analyzer, and importing it to read a list would run whatever else is in it.
Module-level assignments are walked and their right-hand sides passed through
`ast.literal_eval`, so a constant computed at import time is reported as unreadable rather
than guessed at.

The cross-check has an analogue here too, and it is worth keeping for the same reason: the
strict read takes the named constant, and the loose scan pulls every quoted string that
looks like a checker name out of the file. Disagreement means the constant is not the only
place these names live, which is exactly the drift that silently changes a coverage model.
"""

import ast
import re
from typing import Any, Dict, Tuple

from ..repos.base import ConstantCoverageSpec
from .pipeline import CoverageModel, JobGroup, PipelineParseDisagreement, PipelineParseError
from .treeindex import TreeIndex


def parse_constant_model(tree: TreeIndex, spec: ConstantCoverageSpec) -> CoverageModel:
    """Read the coverage constant, cross-check it, and refuse to guess."""
    if not tree.exists(spec.path):
        raise PipelineParseError(f"{spec.path} is not in the tree at {tree.rev[:9]}")

    text = tree.read(spec.path)
    assignments = _assignments(text, spec.path)

    if spec.constant not in assignments:
        raise PipelineParseError(
            f"{spec.path} declares no module-level {spec.constant!r}; it declares "
            f"{sorted(assignments)}. Scout will not fall back to scanning the file for "
            f"something that looks like a coverage list."
        )

    declared = assignments[spec.constant]
    if not isinstance(declared, list):
        raise PipelineParseError(f"{spec.path}:{spec.constant} is a {type(declared).__name__}, not a list")

    aliases = _aliases(assignments.get(spec.alias_constant)) if spec.alias_constant else {}
    lines = _line_index(text)

    groups = []
    for value in declared:
        name = _strip(str(value), spec.strip_suffix)
        groups.append(JobGroup(
            name=name,
            stage=spec.constant,
            template=spec.path,
            family=aliases.get(name, name),
            arch="",
            qualifier="",
            line=lines.get(str(value), 0),
        ))

    model = CoverageModel(
        model=spec.model,
        path=spec.path,
        scope=(spec.constant,),
        job_groups=tuple(groups),
        loose_names=_loose_scan(text, spec.strip_suffix),
        templates_read=(),
        stage_lines={spec.constant: lines.get(spec.constant, 0)},
    )
    if not model.loose_scan_agrees:
        raise PipelineParseDisagreement(
            f"Two reads of {spec.path} disagree. {spec.constant} names {len(model.names)}: "
            f"{list(model.names)}. A loose scan of the file found "
            f"{len(set(model.loose_names))}: {sorted(set(model.loose_names))}. The constant is not the only "
            f"place these names live, so one of the two is stale."
        )
    return model


def _assignments(text: str, path: str) -> Dict[str, Any]:
    """Module-level literal assignments, by name. Parsed, never imported or executed."""
    try:
        module = ast.parse(text, filename=path)
    except SyntaxError as error:
        raise PipelineParseError(f"{path} is not parseable Python: {error}") from error

    found: Dict[str, Any] = {}
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, SyntaxError):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                found[target.id] = value
    return found


def _aliases(mapping: Any) -> Dict[str, str]:
    """Checker name to the thing it actually runs, when the repository states one."""
    if not isinstance(mapping, dict):
        return {}
    aliases = {}
    for key, value in mapping.items():
        if isinstance(value, (list, tuple)) and value:
            aliases[str(key)] = str(value[0])
        elif isinstance(value, str):
            aliases[str(key)] = value
    return aliases


def _strip(value: str, suffix: str) -> str:
    return value[: -len(suffix)] if suffix and value.endswith(suffix) else value


def _loose_scan(text: str, suffix: str) -> Tuple[str, ...]:
    """Every quoted string in the file carrying the checker suffix, wherever it appears."""
    if not suffix:
        return ()
    pattern = re.compile(r"""['"]([A-Za-z0-9][\w.-]*""" + re.escape(suffix) + r""")['"]""")
    return tuple(_strip(name, suffix) for name in pattern.findall(text))


def _line_index(text: str) -> Dict[str, int]:
    lines: Dict[str, int] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        for token in re.findall(r"""['"]([^'"]+)['"]|^([A-Z_]+)\s*=""", line):
            for name in token:
                if name:
                    lines.setdefault(name, number)
    return lines
