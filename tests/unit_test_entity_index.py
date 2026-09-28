"""Counting rules C1 to C4 over synthetic trees, one behaviour at a time.

The conformance suite asserts the rules against a real pinned tree, which proves the
numbers. These prove the edges that a real tree happens not to contain today — a symlink
chain, a cycle, a dangling target, a link escaping the root — and would therefore go
unexercised right up until upstream grows one.
"""

from dataclasses import replace

import pytest

from scout_impl.repos.base import DirectoryEntitySpec, FileEntitySpec
from scout_impl.static.fixtures import TreeFixture
from scout_impl.static.platforms import SymlinkEscapesTree, build_entity_index, build_file_entity_index
from scout_impl.static.treeindex import TreeIndex

SPEC = DirectoryEntitySpec(
    kind="platform",
    family_kind="asic_family",
    root="device",
    declaration_file="platform_asic",
    shared_suffix="_common",
    hwsku_markers=("port_config.ini", "hwsku.json"),
    identity_marker="default_sku",
    max_link_hops=4,
)


def _index(files, spec=SPEC):
    fixture = TreeFixture.from_files(files)
    return build_entity_index(TreeIndex(fixture.source(), fixture.rev), spec)


def test_a_plain_declaration_yields_one_platform_with_one_family():
    index = _index({"device/acme/x86_64-acme_1/platform_asic": "broadcom\n"})
    assert [entity.id for entity in index.entities] == ["acme/x86_64-acme_1"]
    assert index.entities[0].families == ("broadcom",)
    assert index.entities[0].resolved_via == "blob"


def test_c1_follows_a_relative_symlink_by_path_arithmetic():
    index = _index({
        "device/acme/x86_64-acme_1/platform_asic": "mellanox\n",
        "device/acme/x86_64-acme_2/platform_asic": ("120000", "../x86_64-acme_1/platform_asic"),
    })
    second = index.by_id("acme/x86_64-acme_2")
    assert second.families == ("mellanox",)
    assert second.link_hops == 1
    assert second.resolved_via == "symlink"
    assert second.resolved_path == "device/acme/x86_64-acme_1/platform_asic"


def test_c1_follows_a_chain_of_symlinks():
    index = _index({
        "device/acme/a/platform_asic": "centec\n",
        "device/acme/b/platform_asic": ("120000", "../a/platform_asic"),
        "device/acme/c/platform_asic": ("120000", "../b/platform_asic"),
    })
    assert index.by_id("acme/c").families == ("centec",)
    assert index.by_id("acme/c").link_hops == 2


def test_c1_stops_at_the_hop_limit_and_records_the_entity_as_unresolved():
    files = {"device/acme/p0/platform_asic": "broadcom\n"}
    for step in range(1, 8):
        files[f"device/acme/p{step}/platform_asic"] = ("120000", f"../p{step - 1}/platform_asic")
    index = _index(files)

    deepest = index.by_id("acme/p7")
    assert deepest is not None, "an entity over the hop limit is recorded, never dropped"
    assert deepest.families == ()
    # p0 to p4 are within the four-hop limit; p5, p6 and p7 each need more than four.
    assert [item.path for item in index.unresolved] == [
        f"device/acme/p{step}/platform_asic" for step in (5, 6, 7)
    ]
    assert index.by_id("acme/p4").families == ("broadcom",)


def test_c1_records_a_dangling_target_as_unresolved_rather_than_dropping_the_platform():
    index = _index({"device/acme/a/platform_asic": ("120000", "../gone/platform_asic")})
    assert [entity.id for entity in index.entities] == ["acme/a"]
    assert index.by_id("acme/a").families == ()
    assert len(index.unresolved) == 1


def test_c1_records_a_cycle_as_unresolved_rather_than_looping():
    index = _index({
        "device/acme/a/platform_asic": ("120000", "../b/platform_asic"),
        "device/acme/b/platform_asic": ("120000", "../a/platform_asic"),
    })
    assert len(index.entities) == 2
    assert len(index.unresolved) == 2


def test_c1_treats_a_link_escaping_the_repository_root_as_an_error():
    """A best-effort read would answer a different question than the one asked."""
    files = TreeFixture.from_files(
        {"device/acme/a/platform_asic": ("120000", "../../../../outside/platform_asic")}
    )
    with pytest.raises(SymlinkEscapesTree) as raised:
        build_entity_index(TreeIndex(files.source(), files.rev), SPEC)
    assert "above the repository root" in str(raised.value)


def test_c2_parses_a_declaration_to_a_set_never_a_string():
    index = _index({"device/acme/a/platform_asic": "broadcom\nbroadcom-dnx\n"})
    assert index.by_id("acme/a").families == ("broadcom", "broadcom-dnx")
    assert len(index.multi_family) == 1


def test_c2_ignores_blank_lines_and_surrounding_whitespace():
    index = _index({"device/acme/a/platform_asic": "  broadcom \n\n\tmellanox\n\n"})
    assert index.by_id("acme/a").families == ("broadcom", "mellanox")


def test_c3_excludes_a_shared_common_directory_and_reads_it_anyway_for_the_audit():
    """Excluded from the platform set, still resolved, because C2's one case lives there."""
    index = _index({
        "device/acme/x86_64-acme_common/platform_asic": "broadcom\nbroadcom-dnx\n",
        "device/acme/x86_64-acme_1/platform_asic": "broadcom\n",
    })
    assert index.excluded == ("device/acme/x86_64-acme_common",)
    assert [entity.id for entity in index.entities] == ["acme/x86_64-acme_1"]
    assert index.declaration_count == 2
    assert len(index.multi_family) == 1


def test_c4_keeps_a_platform_that_owns_no_hwsku_directory():
    index = _index({
        "device/acme/sup/platform_asic": "broadcom\n",
        "device/acme/leaf/platform_asic": "broadcom\n",
        "device/acme/leaf/HWSKU-A/port_config.ini": "# ports",
    })
    assert sorted(entity.id for entity in index.entities) == ["acme/leaf", "acme/sup"]
    assert [entity.id for entity in index.kept_without_hwsku] == ["acme/sup"]


def test_c4_recognises_a_hwsku_nested_under_a_multi_asic_directory():
    index = _index({
        "device/acme/p/platform_asic": "broadcom\n",
        "device/acme/p/HWSKU-A/asic0/port_config.ini": "# ports",
    })
    assert index.by_id("acme/p").owns_hwsku is True


def test_the_shared_directory_is_not_reached_by_the_owning_lookup_but_links_are():
    index = _index({
        "device/acme/x86_64-acme_common/platform_asic": "broadcom\n",
        "device/acme/a/platform_asic": ("120000", "../x86_64-acme_common/platform_asic"),
    })
    assert index.owning("device/acme/x86_64-acme_common/plugins/thermal.py") is None
    assert index.linking_into("device/acme/x86_64-acme_common/plugins/thermal.py") == ("acme/a",)


def test_the_owning_lookup_maps_a_changed_path_back_to_its_platform():
    index = _index({"device/acme/a/platform_asic": "broadcom\n"})
    assert index.owning("device/acme/a/HWSKU-A/buffers.json.j2").id == "acme/a"
    assert index.owning("files/build_templates/buffers_config.j2") is None


C6_SPEC = replace(SPEC, alias_directories=True, reverse_reach=True)


def test_c6_counts_an_entity_directory_that_is_itself_a_symlink():
    index = _index({
        "device/acme/real/platform_asic": "broadcom-dnx\n",
        "device/acme/real/HWSKU-A/port_config.ini": "# ports",
        "device/acme/alias": ("120000", "real"),
    }, C6_SPEC)

    assert sorted(entity.id for entity in index.entities) == ["acme/alias", "acme/real"]
    assert index.aliases == ("acme/alias",)

    alias = index.by_id("acme/alias")
    assert alias.families == ("broadcom-dnx",), "inherits the target's declaration"
    assert alias.owns_hwsku is True, "and the target's HWSKU ownership"
    assert alias.declaration_path == "device/acme/real/platform_asic", "read from the target"
    assert alias.directory == "device/acme/alias", "but it is its own directory"
    assert alias.resolved_via == "symlink"


def test_an_alias_is_a_platform_and_never_a_declaration():
    """The quantities a reader must not conflate: one tree, two different counts."""
    index = _index({
        "device/acme/real/platform_asic": "broadcom\n",
        "device/acme/alias": ("120000", "real"),
    }, C6_SPEC)

    assert index.declaration_count == 1
    assert len(index.entities) == 2
    assert index.declaration_count - len(index.excluded) + len(index.aliases) == len(index.entities)


def test_an_alias_takes_its_own_architecture_from_its_own_name():
    """It shares data, not identity: a different ONIE name can mean a different CPU."""
    spec = replace(C6_SPEC, arch_prefixes=(("x86_64", "amd64"), ("arm64", "arm64")))
    index = _index({
        "device/acme/x86_64-acme_1/platform_asic": "broadcom\n",
        "device/acme/arm64-acme_1": ("120000", "x86_64-acme_1"),
    }, spec)

    assert index.by_id("acme/x86_64-acme_1").arch == "amd64"
    assert index.by_id("acme/arm64-acme_1").arch == "arm64"


def test_an_alias_pointing_nowhere_is_skipped_rather_than_invented():
    index = _index({
        "device/acme/real/platform_asic": "broadcom\n",
        "device/acme/alias": ("120000", "gone"),
    }, C6_SPEC)
    assert index.aliases == ()
    assert [entity.id for entity in index.entities] == ["acme/real"]


def test_alias_directories_are_off_unless_the_adapter_declares_them():
    index = _index({
        "device/acme/real/platform_asic": "broadcom\n",
        "device/acme/alias": ("120000", "real"),
    })
    assert index.aliases == ()
    assert index.links is None, "and so is reverse reach"


def test_c6_reverse_reach_finds_the_platforms_linking_at_a_changed_file():
    index = _index({
        "device/acme/x86_64-acme_common/pmon.json": "{}",
        "device/acme/a/platform_asic": "broadcom\n",
        "device/acme/a/pmon.json": ("120000", "../x86_64-acme_common/pmon.json"),
        "device/acme/b/platform_asic": "broadcom\n",
        "device/acme/b/pmon.json": ("120000", "../x86_64-acme_common/pmon.json"),
        "device/acme/c/platform_asic": "broadcom\n",
    }, C6_SPEC)

    reached = index.reaching(["device/acme/x86_64-acme_common/pmon.json"])
    assert reached == ("acme/a", "acme/b"), "c links at nothing and is not reached"
    assert index.owning("device/acme/x86_64-acme_common/pmon.json") is None, "nobody owns it"


def test_reverse_reach_follows_a_chain_of_links():
    """Two hops. A single-hop scan of the same tree finds one platform where there are two."""
    index = _index({
        "device/acme/x86_64-acme_common/pmon.json": "{}",
        "device/acme/a/platform_asic": "broadcom\n",
        "device/acme/a/pmon.json": ("120000", "../x86_64-acme_common/pmon.json"),
        "device/acme/b/platform_asic": "broadcom\n",
        "device/acme/b/pmon.json": ("120000", "../a/pmon.json"),
    }, C6_SPEC)
    assert index.reaching(["device/acme/x86_64-acme_common/pmon.json"]) == ("acme/a", "acme/b")


def test_reverse_reach_stops_at_the_hop_limit_rather_than_looping():
    index = _index({
        "device/acme/a/platform_asic": "broadcom\n",
        "device/acme/a/x": ("120000", "../b/x"),
        "device/acme/b/platform_asic": "broadcom\n",
        "device/acme/b/x": ("120000", "../a/x"),
    }, C6_SPEC)
    assert index.reaching(["device/acme/a/x"]) == ()


def test_a_link_to_a_directory_reaches_everything_inside_it():
    """Targets are matched as ancestors too, which is how a whole shared directory works."""
    index = _index({
        "device/acme/x86_64-acme_common/plugins/psu.py": "pass",
        "device/acme/a/platform_asic": "broadcom\n",
        "device/acme/a/plugins": ("120000", "../x86_64-acme_common/plugins"),
    }, C6_SPEC)
    assert index.reaching(["device/acme/x86_64-acme_common/plugins/psu.py"]) == ("acme/a",)


def test_reverse_reach_resolves_the_graph_once_and_in_one_batch():
    files = {"device/acme/x86_64-acme_common/pmon.json": "{}"}
    for name in "abcdefgh":
        files[f"device/acme/{name}/platform_asic"] = "broadcom\n"
        files[f"device/acme/{name}/pmon.json"] = ("120000", "../x86_64-acme_common/pmon.json")
    fixture = TreeFixture.from_files(files)
    tree = TreeIndex(fixture.source(), fixture.rev)
    index = build_entity_index(tree, C6_SPEC)

    before = tree.prefetches
    assert len(index.reaching(["device/acme/x86_64-acme_common/pmon.json"])) == 8
    assert tree.prefetches == before + 1, "one batched prefetch for the whole graph"

    index.reaching(["device/acme/a/pmon.json"])
    assert tree.prefetches == before + 1, "and none for the second question"


def test_the_symlink_graph_costs_nothing_until_something_asks():
    """A whole-tree brief has no changed path, so it must not pay to resolve 1,800 links.

    Indexing still prefetches the declarations and the alias directories, because both are
    needed to count platforms at all. What it must not do is touch the inbound graph, and
    `links.resolved` is the flag that says whether it did.
    """
    files = {"device/acme/a/platform_asic": "broadcom\n"}
    for name in "abcdefgh":
        files[f"device/acme/{name}/pmon.json"] = ("120000", "../x86_64-acme_common/pmon.json")
    fixture = TreeFixture.from_files(files)
    tree = TreeIndex(fixture.source(), fixture.rev)
    index = build_entity_index(tree, C6_SPEC)

    assert index.links.resolved is False
    settled = tree.prefetches
    assert index.reaching([]) == (), "an empty question resolves nothing"
    assert index.links.resolved is False
    assert tree.prefetches == settled

    index.reaching(["device/acme/x86_64-acme_common/pmon.json"])
    assert index.links.resolved is True
    assert tree.prefetches == settled + 1


def test_the_file_entity_model_indexes_files_and_claims_no_counting_rules():
    """The second shape. Reporting zero for a rule that does not apply is the truth."""
    fixture = TreeFixture.from_files({
        "ansible/vars/topo_t0.yml": "topology: {}",
        "ansible/vars/topo_t1-lag.yml": "topology: {}",
        "ansible/vars/docker_registry.yml": "registry: {}",
    })
    spec = FileEntitySpec(
        kind="topology",
        family_kind="topology_type",
        glob="ansible/vars/topo_*.yml",
        name_pattern=r"^ansible/vars/topo_(?P<name>.+)\.yml$",
    )
    index = build_file_entity_index(TreeIndex(fixture.source(), fixture.rev), spec)

    assert sorted(entity.id for entity in index.entities) == ["t0", "t1-lag"]
    assert index.counting_rules is False
    assert index.excluded == ()
    assert index.kept_without_hwsku == ()
    assert index.blobs_read == 0
