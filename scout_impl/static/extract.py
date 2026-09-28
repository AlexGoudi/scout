"""Dispatch an adapter's declared spec to the mechanism that reads it.

Two shapes each, and the second of each was added when the `sonic-mgmt` adapter landed.
That is recorded here rather than smoothed over, because "does a second repository fit
without a core change" is the falsifiable claim the second adapter exists to test (HLD
section 5.1), and the answer is no: `sonic-buildimage` declares entities as directories
carrying a declaration file and coverage as pipeline stages, `sonic-mgmt` declares
entities as files and coverage as a Python constant, and neither pair is a
parameterisation of the other.

What the boundary did hold. The engine, the coverage query, the hotspot ranking, the brief
builder, the schema and the validator all took the second repository unchanged; what had
to grow were two extractor shapes and this two-line dispatch. The adapters themselves stay
declarative.
"""

from ..repos.base import ConstantCoverageSpec, DirectoryEntitySpec, FileEntitySpec, PipelineCoverageSpec
from .constants import parse_constant_model
from .pipeline import CoverageModel, parse_coverage_model
from .platforms import EntityIndex, build_entity_index, build_file_entity_index
from .treeindex import TreeIndex


class UnsupportedSpec(TypeError):
    """An adapter declared a spec shape no extractor implements."""


def build_index(tree: TreeIndex, spec: object) -> EntityIndex:
    if isinstance(spec, DirectoryEntitySpec):
        return build_entity_index(tree, spec)
    if isinstance(spec, FileEntitySpec):
        return build_file_entity_index(tree, spec)
    raise UnsupportedSpec(f"No entity extractor for {type(spec).__name__}")


def build_coverage(tree: TreeIndex, spec: object) -> CoverageModel:
    if isinstance(spec, PipelineCoverageSpec):
        return parse_coverage_model(tree, spec)
    if isinstance(spec, ConstantCoverageSpec):
        return parse_constant_model(tree, spec)
    raise UnsupportedSpec(f"No coverage extractor for {type(spec).__name__}")
