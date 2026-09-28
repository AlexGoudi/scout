"""The adapter boundary: registry, resolution, both repositories' rules, serialization.

Offline throughout. Nothing here reads a checkout except the two tests that identify one
from a tree listing, and those build the listing themselves.
"""

import json

import pytest

from scout_impl.models import FileDiff, PathClass
from scout_impl.repos import (
    PathKind,
    RepoAdapter,
    RepoAdapterError,
    available_adapters,
    detect_in_checkout,
    detect_in_tree,
    get_adapter,
    path_class_from_qualified_id,
    register_adapter,
    resolve_adapter,
)
from scout_impl.repos import sonic_buildimage, sonic_mgmt
from scout_impl.repos.base import CiSurface, EntitySource

SONIC_MGMT = get_adapter("sonic-mgmt")
SONIC_BUILDIMAGE = get_adapter("sonic-buildimage")


# --- registry and resolution -------------------------------------------------------

def test_both_supported_repositories_are_registered() -> None:
    assert available_adapters() == ["sonic-buildimage", "sonic-mgmt"]
    assert get_adapter("sonic-mgmt") is sonic_mgmt.ADAPTER
    assert get_adapter("sonic-buildimage") is sonic_buildimage.ADAPTER


def test_unknown_repository_names_the_ones_that_exist() -> None:
    with pytest.raises(RepoAdapterError) as error:
        get_adapter("sonic-swss")
    assert "sonic-buildimage" in str(error.value)
    assert "sonic-mgmt" in str(error.value)


def test_resolution_by_name_beats_detection() -> None:
    assert resolve_adapter("sonic-buildimage") is SONIC_BUILDIMAGE
    assert resolve_adapter(name="sonic-mgmt", paths=["slave.mk", "Makefile.work"]) is SONIC_MGMT


def test_resolution_without_a_name_or_a_tree_is_an_error() -> None:
    with pytest.raises(RepoAdapterError):
        resolve_adapter()


def test_detection_from_a_tree_listing_identifies_each_repository() -> None:
    assert detect_in_tree(["ansible/testbed-cli.sh", "tests/bgp/test_bgp_fact.py"]) is SONIC_MGMT
    assert detect_in_tree(["slave.mk", "Makefile.work", "rules/config"]) is SONIC_BUILDIMAGE


def test_detection_accepts_a_directory_marker_a_listing_never_names() -> None:
    # A recursive listing names blobs, so `ansible/vars` only exists as a parent of one.
    assert detect_in_tree(["ansible/vars/topo_t0.yml", "tests/common/helpers/assertions.py"]) is SONIC_MGMT


def test_detection_returns_none_for_an_unrelated_tree() -> None:
    assert detect_in_tree(["setup.py", "src/main.rs"]) is None
    with pytest.raises(RepoAdapterError) as error:
        resolve_adapter(paths=["setup.py"])
    assert "sonic-mgmt" in str(error.value)


def test_detection_from_a_checkout_reads_the_filesystem(tmp_path) -> None:
    (tmp_path / "ansible").mkdir()
    (tmp_path / "ansible" / "testbed-cli.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    assert detect_in_checkout(tmp_path) is SONIC_MGMT
    assert resolve_adapter(root=tmp_path) is SONIC_MGMT


def test_ambiguous_detection_is_an_error_rather_than_a_coin_toss() -> None:
    paths = ["ansible/testbed-cli.sh", "slave.mk", "Makefile.work"]
    with pytest.raises(RepoAdapterError) as error:
        detect_in_tree(paths)
    assert "more than one repository" in str(error.value)


def test_a_third_repository_needs_no_change_to_the_core(tmp_path) -> None:
    name = "sonic-swss-unittest"
    only_class = PathClass(repo=name, id="other", rank=1, kind=PathKind.OTHER)
    adapter = RepoAdapter(
        name=name,
        summary="A repository registered by a test",
        markers=(("orchagent/",),),
        path_classes=(only_class,),
        path_rules=(),
        fallback=only_class,
    )
    register_adapter(adapter)
    try:
        assert resolve_adapter(name) is adapter
        assert adapter.classify("anything/at/all.cpp") is only_class
    finally:
        from scout_impl.repos import _ADAPTERS

        del _ADAPTERS[name]


# --- adapter validation ------------------------------------------------------------

def _classes(name: str, count: int):
    return tuple(
        PathClass(repo=name, id=f"c{index}", rank=index + 1, kind=PathKind.OTHER)
        for index in range(count)
    )


def test_ranks_must_be_a_contiguous_run_from_one() -> None:
    name = "gappy"
    classes = (
        PathClass(repo=name, id="a", rank=1, kind=PathKind.CODE),
        PathClass(repo=name, id="b", rank=7, kind=PathKind.OTHER),
    )
    with pytest.raises(RepoAdapterError) as error:
        RepoAdapter(name=name, summary="", markers=(("x",),), path_classes=classes,
                    path_rules=(), fallback=classes[1])
    assert "1..2" in str(error.value)


def test_an_adapter_cannot_claim_another_repository_s_path_class() -> None:
    borrowed = sonic_mgmt.ANSIBLE_CODE
    mine = PathClass(repo="borrower", id="other", rank=2, kind=PathKind.OTHER)
    with pytest.raises(RepoAdapterError) as error:
        RepoAdapter(name="borrower", summary="", markers=(("x",),),
                    path_classes=(borrowed, mine), path_rules=(), fallback=mine)
    assert "owned by another repo" in str(error.value)


def test_a_rule_pointing_at_an_undeclared_class_is_rejected() -> None:
    classes = _classes("stray", 1)
    with pytest.raises(RepoAdapterError) as error:
        RepoAdapter(name="stray", summary="", markers=(("x",),), path_classes=classes,
                    path_rules=(("*.py", sonic_mgmt.ANSIBLE_CODE),), fallback=classes[0])
    assert "undeclared path classes" in str(error.value)


def test_the_fallback_must_be_the_catch_all_kind() -> None:
    only = PathClass(repo="typed", id="code", rank=1, kind=PathKind.CODE)
    with pytest.raises(RepoAdapterError) as error:
        RepoAdapter(name="typed", summary="", markers=(("x",),), path_classes=(only,),
                    path_rules=(), fallback=only)
    assert "must be kind" in str(error.value)


def test_an_adapter_needs_a_name_and_a_marker() -> None:
    classes = _classes("nameless", 1)
    with pytest.raises(RepoAdapterError):
        RepoAdapter(name="", summary="", markers=(("x",),), path_classes=classes,
                    path_rules=(), fallback=classes[0])
    with pytest.raises(RepoAdapterError):
        RepoAdapter(name="markerless", summary="", markers=(), path_classes=_classes("markerless", 1),
                    path_rules=(), fallback=_classes("markerless", 1)[0])


# --- sonic-buildimage classification -----------------------------------------------

@pytest.mark.parametrize(
    "path,expected",
    [
        ("device/nokia/x86_64-nokia_ixr7250e_36x400g-r0/Nokia-IXR7250E-36x400G/port_config.ini",
         sonic_buildimage.PLATFORM_DATA),
        ("device/common/profiles/th3/msft/BALANCED/profile.ini", sonic_buildimage.PLATFORM_DATA),
        ("platform/broadcom/saibcm-modules/Makefile", sonic_buildimage.PLATFORM_DATA),
        ("rules/config", sonic_buildimage.BUILD_RULE),
        ("rules/docker-fpm-frr.mk", sonic_buildimage.BUILD_RULE),
        ("platform/broadcom/sai.mk", sonic_buildimage.BUILD_RULE),
        ("platform/mellanox/mlnx-sai.dep", sonic_buildimage.BUILD_RULE),
        ("slave.mk", sonic_buildimage.BUILD_RULE),
        ("Makefile.work", sonic_buildimage.BUILD_RULE),
        ("scripts/build_kvm_image.sh", sonic_buildimage.BUILD_RULE),
        ("sonic-slave-bookworm/Dockerfile.j2", sonic_buildimage.BUILD_RULE),
        ("onie-image.conf", sonic_buildimage.BUILD_RULE),
        ("build_debian.sh", sonic_buildimage.BUILD_RULE),
        ("files/build/versions/default/versions-deb-bookworm", sonic_buildimage.VERSION_PIN),
        ("versions/dockers/docker-orchagent/versions-py3", sonic_buildimage.VERSION_PIN),
        ("files/build_templates/init_cfg.json.j2", sonic_buildimage.IMAGE_TEMPLATE),
        ("files/build_templates/sonic_debian_extension.j2", sonic_buildimage.IMAGE_TEMPLATE),
        ("files/image_config/interfaces/interfaces-config.sh", sonic_buildimage.IMAGE_CONFIG),
        ("files/initramfs-tools/union-mount", sonic_buildimage.IMAGE_CONFIG),
        ("files/Aboot/boot0.j2", sonic_buildimage.IMAGE_CONFIG),
        ("installer/install.sh", sonic_buildimage.IMAGE_CONFIG),
        ("dockers/docker-orchagent/Dockerfile.j2", sonic_buildimage.CONTAINER),
        ("dockers/docker-fpm-frr/frr.sh", sonic_buildimage.CONTAINER),
        ("src/sonic-config-engine/setup.py", sonic_buildimage.COMPONENT_SOURCE),
        (".gitmodules", sonic_buildimage.COMPONENT_SOURCE),
        (".azure-pipelines/azure-pipelines-build.yml", sonic_buildimage.PIPELINE),
        (".github/workflows/automerge.yml", sonic_buildimage.PIPELINE),
        ("azure-pipelines.yml", sonic_buildimage.PIPELINE),
        ("README.md", sonic_buildimage.DOCUMENTATION),
        ("dockers/docker-fpm-frr/README.md", sonic_buildimage.DOCUMENTATION),
        ("check_install.py", sonic_buildimage.OTHER),
    ],
)
def test_buildimage_classification(path: str, expected: PathClass) -> None:
    assert SONIC_BUILDIMAGE.classify(path) is expected


def test_buildimage_build_rules_outrank_everything_they_build() -> None:
    build = SONIC_BUILDIMAGE.classify("rules/sonic-utilities.mk")
    platform = SONIC_BUILDIMAGE.classify("device/arista/x86_64-arista_7050_qx32/hwsku.json")
    component = SONIC_BUILDIMAGE.classify("src/sonic-utilities/sonic_package_manager/main.py")
    assert build.rank < platform.rank < component.rank


def test_buildimage_leading_dot_slash_is_normalized_the_same_way() -> None:
    assert SONIC_BUILDIMAGE.classify("./rules/config") is sonic_buildimage.BUILD_RULE


# --- the repo-neutral half ----------------------------------------------------------

def test_every_adapter_shares_the_documentation_and_catch_all_convention() -> None:
    for adapter in (SONIC_MGMT, SONIC_BUILDIMAGE):
        ranked = adapter.ranked_classes()
        assert ranked[-1].kind is PathKind.DOCUMENTATION, adapter.name
        assert ranked[-2] is adapter.fallback, adapter.name
        assert adapter.fallback.kind is PathKind.OTHER, adapter.name


def test_ranking_needs_no_knowledge_of_which_repo_produced_the_classes() -> None:
    # The prefilter sorts a mixed list on rank alone; that it can is the whole point of
    # keeping rank on the class rather than in a per-repo table.
    mixed = [
        SONIC_MGMT.classify("tests/bgp/test_bgp_fact.py"),
        SONIC_BUILDIMAGE.classify("rules/config"),
        SONIC_MGMT.classify("ansible/library/topo_facts.py"),
        SONIC_BUILDIMAGE.classify("README.md"),
    ]
    widest_first = sorted(mixed, key=lambda path_class: path_class.rank)
    assert widest_first[0] is sonic_buildimage.BUILD_RULE
    assert widest_first[-1] is sonic_buildimage.DOCUMENTATION


def test_documentation_is_recognizable_across_repositories_by_kind() -> None:
    documentation = {
        adapter.classify("docs/whatever.md").kind
        for adapter in (SONIC_MGMT, SONIC_BUILDIMAGE)
    }
    assert documentation == {PathKind.DOCUMENTATION}


# --- extension points the later work fills -----------------------------------------

def test_entity_sources_are_declared_as_globs_over_the_tree() -> None:
    topology = SONIC_MGMT.entity_source("topology")
    assert topology is not None
    assert topology.globs == ("ansible/vars/topo_*.yml",)

    hwsku = SONIC_BUILDIMAGE.entity_source("hwsku")
    assert hwsku is not None
    assert hwsku.globs == ("device/*/port_config.ini",)
    assert SONIC_BUILDIMAGE.entity_source("no-such-kind") is None


def test_every_entity_source_and_ci_surface_is_well_formed() -> None:
    for adapter in (SONIC_MGMT, SONIC_BUILDIMAGE):
        for source in adapter.entity_sources:
            assert isinstance(source, EntitySource)
            assert source.kind and source.globs and source.description
        for surface in adapter.ci_surfaces:
            assert isinstance(surface, CiSurface)
            assert surface.name and surface.config_path


def test_ci_surfaces_point_at_the_config_rather_than_copying_it() -> None:
    surface = SONIC_MGMT.ci_surfaces[0]
    assert surface.config_path == ".azure-pipelines/impacted_area_testing/constant.py"


def test_the_invariant_table_exists_and_is_empty_until_the_detectors_land() -> None:
    assert SONIC_MGMT.invariants == ()
    assert SONIC_BUILDIMAGE.invariants == ()


# --- serialization -----------------------------------------------------------------

def test_a_path_class_serializes_to_a_self_describing_id() -> None:
    assert sonic_mgmt.ANSIBLE_CODE.qualified_id == "sonic-mgmt:ansible_code"
    assert sonic_buildimage.PLATFORM_DATA.qualified_id == "sonic-buildimage:platform_data"
    assert path_class_from_qualified_id("sonic-buildimage:platform_data") is sonic_buildimage.PLATFORM_DATA


def test_the_same_class_id_in_two_repositories_stays_distinct() -> None:
    # Both repositories have a `pipeline` and an `other`; without the qualifier a
    # serialized change set could not tell them apart, and their ranks differ.
    assert sonic_mgmt.PIPELINE != sonic_buildimage.PIPELINE
    assert sonic_mgmt.PIPELINE.rank != sonic_buildimage.PIPELINE.rank
    assert path_class_from_qualified_id("sonic-mgmt:pipeline") is sonic_mgmt.PIPELINE
    assert path_class_from_qualified_id("sonic-buildimage:pipeline") is sonic_buildimage.PIPELINE


def test_an_unqualified_or_unknown_path_class_is_rejected() -> None:
    with pytest.raises(RepoAdapterError) as error:
        path_class_from_qualified_id("ansible_code")
    assert "REPO:CLASS_ID" in str(error.value)

    with pytest.raises(RepoAdapterError):
        path_class_from_qualified_id("sonic-mgmt:platform_data")


def test_a_file_diff_round_trips_its_path_class_through_json() -> None:
    original = FileDiff(
        path="device/nokia/x86_64-nokia_ixr7250e_36x400g-r0/platform.json",
        change_type="modified",
        path_class=SONIC_BUILDIMAGE.classify("device/nokia/x/platform.json"),
    )
    restored = FileDiff.from_dict(json.loads(json.dumps(original.to_dict())))

    assert restored.path_class is sonic_buildimage.PLATFORM_DATA
    assert restored.to_dict() == original.to_dict()
