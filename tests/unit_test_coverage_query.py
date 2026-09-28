"""The coverage-gap query: three sets, and the refusal to collapse them into two."""

from scout_impl.repos.base import DirectoryEntitySpec
from scout_impl.static.coverage import (
    ARCHITECTURE_AWARE,
    STRING_EQUALITY,
    affected_entities,
    entities_declaring,
    families_of,
    query_coverage,
)
from scout_impl.static.fixtures import TreeFixture
from scout_impl.static.pipeline import CoverageModel, JobGroup
from scout_impl.static.platforms import build_entity_index
from scout_impl.static.treeindex import TreeIndex

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
    "device/acme/built/platform_asic": "broadcom\n",
    "device/acme/unbuilt/platform_asic": "centec\n",
    "device/acme/ambiguous/platform_asic": "marvell-prestera\n",
    "device/acme/dual/platform_asic": "centec\nbroadcom\n",
    "device/acme/silent/platform_asic": ("120000", "../gone/platform_asic"),
}

MODEL = CoverageModel(
    model="pr-build-stage",
    path="azure-pipelines.yml",
    scope=("Build",),
    job_groups=(
        JobGroup(name="broadcom", stage="Build", template="t", family="broadcom", arch="amd64"),
        JobGroup(name="marvell-prestera-arm64", stage="Build", template="t",
                 family="marvell-prestera", arch="arm64", qualifier="arm64"),
    ),
    loose_names=("broadcom", "marvell-prestera-arm64"),
    templates_read=(),
)


def _index(files=None):
    fixture = TreeFixture.from_files(files or TREE)
    return build_entity_index(TreeIndex(fixture.source(), fixture.rev), SPEC)


def test_the_three_sets_partition_the_affected_set():
    result = query_coverage(_index(), MODEL)
    assert result.covered == ("acme/built", "acme/dual")
    assert result.ambiguous == ("acme/ambiguous",)
    assert result.uncovered == ("acme/silent", "acme/unbuilt")
    assert result.is_exhaustive


def test_c5_covers_a_platform_when_any_of_its_families_is_built():
    """`acme/dual` declares centec, which nothing builds, and broadcom, which does."""
    result = query_coverage(_index(), MODEL)
    assert "acme/dual" in result.covered
    assert result.matched_by["acme/dual"] == ("broadcom",)


def test_both_candidate_answers_are_reported_and_neither_is_chosen():
    result = query_coverage(_index(), MODEL)
    assert result.uncovered_under == {STRING_EQUALITY: 3, ARCHITECTURE_AWARE: 2}


def test_an_unresolvable_declaration_is_uncovered_and_flagged_as_unknown():
    """A platform whose family Scout could not read is not quietly treated as built."""
    result = query_coverage(_index(), MODEL)
    assert "acme/silent" in result.uncovered
    assert result.unknown == ("acme/silent",)


def test_a_group_with_no_qualifier_covers_its_family_outright():
    model = CoverageModel(
        model="m", path="p", scope=("s",),
        job_groups=(JobGroup(name="t1", stage="s", template="t", family="t1-lag", arch=""),),
        loose_names=("t1",), templates_read=(),
    )
    assert set(model.built_families) == {"t1", "t1-lag"}
    assert model.alias_families == {}


def test_a_qualified_group_covers_its_own_name_but_only_maybe_its_family():
    assert set(MODEL.built_families) == {"broadcom", "marvell-prestera-arm64"}
    assert MODEL.alias_families == {"marvell-prestera": ("marvell-prestera-arm64",)}


def test_narrowing_to_a_change_set_keeps_the_partition_exhaustive():
    index = _index()
    result = query_coverage(index, MODEL, ["acme/unbuilt"])
    assert result.affected == ("acme/unbuilt",)
    assert result.uncovered == ("acme/unbuilt",)
    assert result.covered == () and result.ambiguous == ()
    assert result.is_exhaustive


def test_an_affected_entity_the_index_does_not_know_is_uncovered_not_dropped():
    result = query_coverage(_index(), MODEL, ["acme/built", "acme/invented"])
    assert result.uncovered == ("acme/invented",)
    assert result.unknown == ("acme/invented",)
    assert result.is_exhaustive


def test_changed_paths_resolve_to_the_entities_that_own_them():
    index = _index()
    reached = affected_entities(index, [
        "device/acme/built/HWSKU-A/port_config.ini",
        "device/acme/unbuilt/platform_asic",
        "files/build_templates/init_cfg.json.j2",
    ])
    assert reached == ("acme/built", "acme/unbuilt")
    assert families_of(index, reached) == ("broadcom", "centec")


def test_entities_declaring_a_family_are_enumerable_for_the_finding():
    index = _index()
    assert [entity.id for entity in entities_declaring(index, "centec")] == ["acme/dual", "acme/unbuilt"]
