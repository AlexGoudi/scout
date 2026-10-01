"""The pipeline parser, against both pinned trees.

The parser reads 9 job groups upstream and 8 on the fork, and its strict and loose parses
agree on both. The failure it guards against is silent: a coverage model can be wrong in a
way that changes every finding while the citations still resolve, the brief still
validates and the report still renders. The cross-check exists for that reason, and
`test_a_build_only_scope_disagrees_with_the_whole_file_scan` drives it to raise with a
scope narrower than the pipeline.

Four properties are asserted: the job-group list for both trees, that the strict parse
resolves template indirection rather than scanning, that a missing stage is fatal instead
of falling back, and that a strict-versus-loose disagreement raises.
"""

import pytest

from scout_impl.repos.base import PipelineCoverageSpec
from scout_impl.static.pipeline import (
    PipelineParseDisagreement,
    PipelineStageNotFound,
    loose_scan,
    parse_coverage_model,
    strict_parse,
)

BUILD_ONLY = PipelineCoverageSpec(
    model="pr-build-stage",
    path="azure-pipelines.yml",
    stages=("Build",),
    family_variable="PLATFORM_NAME",
    arch_variable="PLATFORM_ARCH",
    default_arch="amd64",
)

UPSTREAM_JOB_GROUPS = (
    "alpinevs",
    "aspeed-arm64",
    "broadcom",
    "marvell-prestera-arm64",
    "marvell-prestera-armhf",
    "mellanox",
    "nvidia-bluefield",
    "vpp",
    "vs",
)


def test_upstream_has_nine_build_job_groups(upstream):
    assert upstream.model.names == UPSTREAM_JOB_GROUPS
    assert len(upstream.model.names) == 9


def test_upstream_strict_and_loose_parses_agree(upstream):
    assert upstream.model.loose_scan_agrees is True
    assert sorted(set(upstream.model.loose_names)) == list(UPSTREAM_JOB_GROUPS)


def test_the_fork_has_eight_job_groups_of_which_five_are_in_the_build_stage(fork):
    assert len(fork.model.names) == 8
    in_build = sorted(group.name for group in fork.model.job_groups if group.stage == "Build")
    assert in_build == [
        "broadcom",
        "marvell-prestera-arm64",
        "marvell-prestera-armhf",
        "mellanox",
        "nvidia-bluefield",
    ]


def test_a_build_only_scope_disagrees_with_the_whole_file_scan(fork):
    """A scope narrower than the pipeline is caught, which is the guard worth keeping.

    Scoping to the stage literally named `Build` finds 5 job groups on this tree while the
    whole file holds 8, because `BuildVS` builds `vs`, `vpp` and `alpinevs` and runs on
    every pull request too. A coverage model missing three job groups is wrong in a way
    nothing downstream notices, so the cross-check refuses it rather than recording it.

    This is a configuration mistake the test provokes on purpose, not a defect that once
    escaped: the shipped scope is both stages, and on this tree it agrees with the scan.
    """
    scoped = strict_parse(fork.tree, BUILD_ONLY)
    assert len(scoped.names) == 5
    assert len(set(scoped.loose_names)) == 8
    assert scoped.loose_scan_agrees is False

    with pytest.raises(PipelineParseDisagreement) as raised:
        parse_coverage_model(fork.tree, BUILD_ONLY)
    assert "5" in str(raised.value) and "8" in str(raised.value)

    assert fork.model.loose_scan_agrees is True, "the scope Scout actually ships agrees"


def test_a_missing_stage_fails_loudly_instead_of_scanning_the_whole_file(upstream):
    absent = PipelineCoverageSpec(model="pr-build-stage", path="azure-pipelines.yml", stages=("NoSuchStage",))
    with pytest.raises(PipelineStageNotFound) as raised:
        strict_parse(upstream.tree, absent)
    assert "NoSuchStage" in str(raised.value)
    assert "will not fall back" in str(raised.value)


def test_the_parse_resolves_template_indirection_rather_than_grepping_for_names(upstream):
    """The job groups are a parameter to a template chain, not entries in a job list."""
    assert upstream.model.templates_read == (
        ".azure-pipelines/azure-pipelines-build.yml",
        ".azure-pipelines/azure-pipelines-image-template.yml",
        ".azure-pipelines/azure-pipelines-job-groups.yml",
    )


def test_the_template_default_job_group_list_is_not_mistaken_for_pr_coverage(upstream):
    """`azure-pipelines-build.yml` carries the official-build default behind an `if`.

    Selecting it would report `barefoot`, `marvell-teralynx` and the rest as PR coverage,
    which is the failure that changes every number while nothing errors.
    """
    default_only = {"barefoot", "marvell-teralynx", "centec", "nephos", "pensando"}
    assert default_only.isdisjoint(set(upstream.model.names))


def test_the_parse_is_published_in_full_so_a_reviewer_can_check_it(upstream):
    record = upstream.model.as_parse_record()
    assert record["scope"] == "Build+BuildVS"
    assert record["strict"] is True
    assert record["loose_scan_agrees"] is True
    assert record["loose_scan_names"] == list(UPSTREAM_JOB_GROUPS)
    assert record["limitations"] == []


def test_the_loose_scan_shares_no_code_with_the_strict_parse(upstream):
    """It is a line-oriented regex that never loads YAML, which is what makes it a check."""
    text = upstream.tree.read("azure-pipelines.yml")
    assert sorted(set(loose_scan(text))) == list(UPSTREAM_JOB_GROUPS)


def test_only_groups_that_declare_their_own_architecture_are_qualified(upstream):
    qualified = {group.name: group.family for group in upstream.model.job_groups if group.is_arch_qualified}
    assert qualified == {
        "aspeed-arm64": "aspeed",
        "marvell-prestera-arm64": "marvell-prestera",
        "marvell-prestera-armhf": "marvell-prestera",
    }
    # nvidia-bluefield states PLATFORM_ARCH too, but its name is its family, so there is
    # nothing undetermined about it and it must not land in the ambiguous set.
    bluefield = upstream.model.group("nvidia-bluefield")
    assert bluefield.arch == "arm64"
    assert bluefield.is_arch_qualified is False


def test_every_job_group_carries_a_line_number_for_its_citation(upstream):
    for group in upstream.model.job_groups:
        assert group.line > 0
