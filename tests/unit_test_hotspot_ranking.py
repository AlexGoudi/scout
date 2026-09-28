"""Hotspot ranking: the score breakdown, and that it adds up.

The schema's contract is that the components sum to the score. Floating point makes that
a real assertion rather than a tautology, which is why the builder rounds the components
and then sums the rounded values instead of rounding a total.
"""

from scout_impl.models import CHANGE_MODIFIED, FileDiff
from scout_impl.repos import get_adapter
from scout_impl.repos.base import DirectoryEntitySpec
from scout_impl.static.coverage import query_coverage
from scout_impl.static.fixtures import TreeFixture
from scout_impl.static.hotspots import rank_hotspots
from scout_impl.static.pipeline import CoverageModel, JobGroup
from scout_impl.static.platforms import build_entity_index
from scout_impl.static.treeindex import TreeIndex

ADAPTER = get_adapter("sonic-buildimage")

SPEC = DirectoryEntitySpec(
    kind="platform",
    family_kind="asic_family",
    root="device",
    declaration_file="platform_asic",
    shared_suffix="_common",
    hwsku_markers=("port_config.ini",),
    identity_marker="default_sku",
)

TREE = {
    "device/acme/x86_64-acme_common/platform_asic": "broadcom\n",
    "device/acme/built/platform_asic": "broadcom\n",
    "device/acme/unbuilt/platform_asic": "centec\n",
    "device/acme/linked/platform_asic": ("120000", "../x86_64-acme_common/platform_asic"),
}

MODEL = CoverageModel(
    model="pr-build-stage",
    path="azure-pipelines.yml",
    scope=("Build",),
    job_groups=(JobGroup(name="broadcom", stage="Build", template="t", family="broadcom", arch="amd64"),),
    loose_names=("broadcom",),
    templates_read=(),
)


def _setup():
    fixture = TreeFixture.from_files(TREE)
    index = build_entity_index(TreeIndex(fixture.source(), fixture.rev), SPEC)
    return index, query_coverage(index, MODEL)


def _diff(path, class_id):
    return FileDiff(path=path, change_type=CHANGE_MODIFIED, path_class=ADAPTER.path_class(class_id))


def test_every_component_sums_to_the_score():
    index, coverage = _setup()
    files = [
        _diff("device/acme/unbuilt/platform_asic", "platform_data"),
        _diff("rules/config", "build_rule"),
        _diff("README.md", "documentation"),
    ]
    for hotspot in rank_hotspots(files, index, coverage, len(ADAPTER.path_classes)):
        assert round(sum(hotspot.score_breakdown.values()), 9) == hotspot.score


def test_the_breakdown_names_the_four_signals_the_schema_requires():
    index, coverage = _setup()
    hotspot = rank_hotspots([_diff("device/acme/unbuilt/platform_asic", "platform_data")],
                            index, coverage, len(ADAPTER.path_classes))[0]
    assert set(hotspot.score_breakdown) == {"path_class", "entity_fanout", "coverage_gap", "ambiguity"}


def test_a_change_reaching_an_unbuilt_platform_outscores_one_reaching_a_built_platform():
    """The coverage gap is the point: identical files, opposite sides of the line."""
    index, coverage = _setup()
    ranked = rank_hotspots(
        [_diff("device/acme/unbuilt/platform_asic", "platform_data"),
         _diff("device/acme/built/platform_asic", "platform_data")],
        index, coverage, len(ADAPTER.path_classes),
    )
    assert ranked[0].path == "device/acme/unbuilt/platform_asic"
    assert ranked[0].score_breakdown["coverage_gap"] > 0
    assert ranked[1].score_breakdown["coverage_gap"] == 0


def test_shared_infrastructure_outranks_a_leaf_platform_file_of_the_same_reach():
    index, coverage = _setup()
    ranked = rank_hotspots(
        [_diff("device/acme/built/platform_asic", "platform_data"), _diff("rules/config", "build_rule")],
        index, coverage, len(ADAPTER.path_classes),
    )
    assert ranked[0].path == "rules/config"


def test_a_change_to_a_shared_directory_reaches_the_platforms_that_link_into_it():
    """C3 excluded the directory; it did not stop a change there from reaching hardware."""
    index, coverage = _setup()
    hotspot = rank_hotspots([_diff("device/acme/x86_64-acme_common/plugins/psu.py", "platform_data")],
                            index, coverage, len(ADAPTER.path_classes))[0]
    assert hotspot.entities == ("asic_family:broadcom", "platform:acme/linked")
    assert hotspot.score_breakdown["entity_fanout"] > 0


def test_hotspots_are_ranked_highest_first_and_capped():
    index, coverage = _setup()
    files = [_diff(f"device/acme/unbuilt/file{n}.j2", "platform_data") for n in range(15)]
    ranked = rank_hotspots(files, index, coverage, len(ADAPTER.path_classes), limit=10)
    assert len(ranked) == 10
    assert [item.id for item in ranked] == [f"h-{n:03d}" for n in range(1, 11)]
    assert ranked == tuple(sorted(ranked, key=lambda item: (-item.score, item.path)))


def test_a_path_reaching_no_entity_still_scores_on_its_path_class_alone():
    index, coverage = _setup()
    hotspot = rank_hotspots([_diff("rules/config", "build_rule")], index, coverage,
                            len(ADAPTER.path_classes))[0]
    assert hotspot.entities == ()
    assert hotspot.score_breakdown["entity_fanout"] == 0
    assert hotspot.score == hotspot.score_breakdown["path_class"] > 0


def test_no_score_can_exceed_one():
    index, coverage = _setup()
    for hotspot in rank_hotspots(
        [_diff("device/acme/unbuilt/platform_asic", "build_rule")], index, coverage, 1
    ):
        assert 0 <= hotspot.score <= 1
