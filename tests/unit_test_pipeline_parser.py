"""The pipeline parser over synthetic pipelines: indirection, scoping and the cross-check.

The conformance suite runs this parser against two real trees. These tests isolate the
behaviours that make the real result right, so a regression says which property broke
rather than only that a number moved.
"""

import pytest

from scout_impl.repos.base import ConstantCoverageSpec, PipelineCoverageSpec
from scout_impl.static.constants import parse_constant_model
from scout_impl.static.pipeline import (
    PipelineParseDisagreement,
    PipelineParseError,
    PipelineStageNotFound,
    loose_scan,
    parse_coverage_model,
    strict_parse,
)
from scout_impl.static.fixtures import TreeFixture
from scout_impl.static.treeindex import TreeIndex

SPEC = PipelineCoverageSpec(
    model="pr-build-stage",
    path="azure-pipelines.yml",
    stages=("Build",),
    family_variable="PLATFORM_NAME",
    arch_variable="PLATFORM_ARCH",
    default_arch="amd64",
)

# The shape upstream uses: the stage delegates to a template, the template forwards the
# caller's job groups when it got any and substitutes a much larger default when it did not.
BUILD_TEMPLATE = """
parameters:
- name: 'jobGroups'
  type: object
  default: ''
jobs:
- template: nested-job-groups.yml
  parameters:
    ${{ if ne(parameters.jobGroups, '') }}:
      jobGroups: ${{ parameters.jobGroups }}
    ${{ if eq(parameters.jobGroups, '') }}:
      jobGroups:
      - name: official-only-a
      - name: official-only-b
"""

NESTED_TEMPLATE = """
parameters:
- name: 'jobGroups'
  type: object
  default: []
jobs:
- ${{ each jobGroup in parameters.jobGroups }}:
  - job: ${{ jobGroup.name }}
"""

PIPELINE = """
stages:
- stage: Build
  jobs:
  - template: .azure-pipelines/build.yml
    parameters:
      jobGroups:
      - name: broadcom
      - name: marvell-prestera-arm64
        variables:
          PLATFORM_NAME: marvell-prestera
          PLATFORM_ARCH: arm64
- stage: Test
  jobs:
  - job: vstest
"""


def _tree(pipeline=PIPELINE, build=BUILD_TEMPLATE, nested=NESTED_TEMPLATE):
    fixture = TreeFixture.from_files({
        "azure-pipelines.yml": pipeline,
        ".azure-pipelines/build.yml": build,
        ".azure-pipelines/nested-job-groups.yml": nested,
    })
    return TreeIndex(fixture.source(), fixture.rev)


def test_the_parse_follows_the_template_chain_to_the_jobs_actually_scheduled():
    model = parse_coverage_model(_tree(), SPEC)
    assert model.names == ("broadcom", "marvell-prestera-arm64")
    assert model.templates_read == (".azure-pipelines/build.yml", ".azure-pipelines/nested-job-groups.yml")


def test_the_templates_own_default_job_groups_are_not_reported_as_coverage():
    """The `if eq(parameters.jobGroups, '')` branch holds the official-build set."""
    model = parse_coverage_model(_tree(), SPEC)
    assert "official-only-a" not in model.names
    assert "official-only-b" not in model.names


def test_a_template_default_is_used_when_the_caller_passes_nothing():
    pipeline = """
stages:
- stage: Build
  jobs:
  - template: .azure-pipelines/build.yml
"""
    model = strict_parse(_tree(pipeline=pipeline), SPEC)
    assert model.names == ("official-only-a", "official-only-b")


def test_a_job_group_records_the_family_and_architecture_the_pipeline_states():
    model = parse_coverage_model(_tree(), SPEC)
    qualified = model.group("marvell-prestera-arm64")
    assert qualified.family == "marvell-prestera"
    assert qualified.arch == "arm64"
    assert qualified.is_arch_qualified is True

    plain = model.group("broadcom")
    assert plain.family == "broadcom"
    assert plain.arch == "amd64"
    assert plain.is_arch_qualified is False


def test_a_group_renamed_without_its_own_architecture_is_not_ambiguous():
    """Two names for one thing is not the same as one name for two architectures."""
    pipeline = PIPELINE.replace("          PLATFORM_ARCH: arm64\n", "")
    model = parse_coverage_model(_tree(pipeline=pipeline), SPEC)
    assert model.group("marvell-prestera-arm64").is_arch_qualified is False
    assert "marvell-prestera" in model.built_families
    assert model.alias_families == {}


def test_a_missing_stage_raises_instead_of_scanning_the_whole_file():
    spec = PipelineCoverageSpec(model="m", path="azure-pipelines.yml", stages=("Release",))
    with pytest.raises(PipelineStageNotFound) as raised:
        strict_parse(_tree(), spec)
    assert "Release" in str(raised.value)


def test_a_file_with_no_stages_list_is_not_a_pipeline_scout_will_scope():
    tree = _tree(pipeline="parameters:\n- name: x\n")
    with pytest.raises(PipelineParseError) as raised:
        strict_parse(tree, SPEC)
    assert "no `stages` list" in str(raised.value)


def test_unparseable_yaml_is_reported_as_such():
    tree = _tree(pipeline="stages:\n  - stage: Build\n   bad indentation\n")
    with pytest.raises(PipelineParseError) as raised:
        strict_parse(tree, SPEC)
    assert "not loadable YAML" in str(raised.value)


def test_the_loose_scan_finds_job_groups_anywhere_in_the_file():
    names = loose_scan(PIPELINE)
    assert sorted(set(names)) == ["broadcom", "marvell-prestera-arm64"]


def test_the_loose_scan_ignores_parameter_and_variable_names():
    text = """
parameters:
- name: TIMEOUT
variables:
- name: CACHE_MODE
stages:
- stage: Build
  jobs:
  - template: t.yml
    parameters:
      jobGroups:
      - name: broadcom
"""
    assert sorted(set(loose_scan(text))) == ["broadcom"]


def test_a_strict_and_loose_disagreement_raises_rather_than_being_recorded_quietly():
    """A second job group in a second stage, outside the strict scope. Risk R3, in miniature."""
    pipeline = PIPELINE + """
- stage: BuildVS
  jobs:
  - template: .azure-pipelines/build.yml
    parameters:
      jobGroups:
      - name: vs
"""
    tree = _tree(pipeline=pipeline)
    scoped = strict_parse(tree, SPEC)
    assert len(scoped.names) == 2
    assert len(set(scoped.loose_names)) == 3
    assert scoped.loose_scan_agrees is False

    with pytest.raises(PipelineParseDisagreement):
        parse_coverage_model(tree, SPEC)

    widened = PipelineCoverageSpec(
        model="m", path="azure-pipelines.yml", stages=("Build", "BuildVS"),
        family_variable="PLATFORM_NAME", arch_variable="PLATFORM_ARCH", default_arch="amd64",
    )
    assert parse_coverage_model(tree, widened).names == ("broadcom", "marvell-prestera-arm64", "vs")


def test_a_condition_the_parser_cannot_evaluate_excludes_its_branch_and_is_reported():
    """Reporting nothing is the safe direction: an invented job group inflates coverage.

    With the forwarding branch behind a condition the parser does not model, the caller's
    job groups never reach the nested template and the result is empty rather than the
    template's official-build default. Empty is visibly wrong; the default would look
    plausible and be wrong by 30 platforms.
    """
    build = BUILD_TEMPLATE.replace("ne(parameters.jobGroups, '')", "contains(variables.x, 'y')")
    model = strict_parse(_tree(build=build), SPEC)
    assert model.names == ()
    assert any("condition not evaluated" in note for note in model.limitations)
    assert model.loose_scan_agrees is False, "and the cross-check catches it"


def test_a_template_in_another_repository_is_recorded_rather_than_followed():
    pipeline = """
stages:
- stage: Build
  jobs:
  - template: pr_test_template.yml@sonic-mgmt
"""
    model = strict_parse(_tree(pipeline=pipeline), SPEC)
    assert model.names == ()
    assert any("another repository" in note for note in model.limitations)


CONSTANTS = """
PR_TOPOLOGY_TYPE = ["t0_checker", "t1_checker"]

PR_CHECKER_TOPOLOGY_NAME = {
    "t0": ["t0", "kvmtest-t0_"],
    "t1": ["t1-lag", "kvmtest-t1-lag_"],
}

MAX_INSTANCE_NUMBER = 40
"""

CONSTANT_SPEC = ConstantCoverageSpec(
    model="pr-checker-topology",
    path="constant.py",
    constant="PR_TOPOLOGY_TYPE",
    strip_suffix="_checker",
    alias_constant="PR_CHECKER_TOPOLOGY_NAME",
)


def _constants_tree(text=CONSTANTS):
    fixture = TreeFixture.from_files({"constant.py": text})
    return TreeIndex(fixture.source(), fixture.rev)


def test_a_coverage_constant_is_parsed_not_imported():
    model = parse_constant_model(_constants_tree(), CONSTANT_SPEC)
    assert model.names == ("t0", "t1")
    assert model.group("t1").family == "t1-lag"
    assert model.group("t1").is_arch_qualified is False
    assert "t1-lag" in model.built_families


def test_a_coverage_constant_that_is_absent_fails_loudly():
    with pytest.raises(PipelineParseError) as raised:
        parse_constant_model(_constants_tree("OTHER = [1]\n"), CONSTANT_SPEC)
    assert "will not fall back" in str(raised.value)


def test_a_coverage_constant_that_is_computed_rather_than_literal_is_not_guessed_at():
    text = "PREFIX = 't0'\nPR_TOPOLOGY_TYPE = [PREFIX + '_checker']\n"
    with pytest.raises(PipelineParseError):
        parse_constant_model(_constants_tree(text), CONSTANT_SPEC)


def test_a_checker_name_living_outside_the_constant_is_a_disagreement():
    text = CONSTANTS + '\nLEGACY = ["m0_checker"]\n'
    with pytest.raises(PipelineParseDisagreement) as raised:
        parse_constant_model(_constants_tree(text), CONSTANT_SPEC)
    assert "m0" in str(raised.value)
