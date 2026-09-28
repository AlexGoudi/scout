"""Counting rules C1 to C4, asserted one at a time against a pinned tree.

The rules are normative (HLD section 4.3.1) and every number Scout reports about platform
coverage is a consequence of them, so each gets its own test rather than being implied by
the headline. A single test asserting 284 would pass with two rules wrong in opposite
directions.

Everything here is pinned to `62cfe5086`, the last commit on upstream master on
21 Sep 2026 whose tree holds 20,155 paths. Asserting against live master would make the
suite fail every time somebody adds a platform, which teaches the team to ignore it.
"""

ARISTA_COMMON = "device/arista/x86_64-arista_common"
ARISTA_7800_SUP = "arista/x86_64-arista_7800_sup"


def test_the_pinned_tree_is_the_one_the_documents_were_measured_against(upstream):
    assert upstream.rev == "62cfe5086e4ffae105c506093475c3a50424bb4c"
    assert upstream.tree.total_paths == 20155


def test_the_tree_declares_287_platform_asic_files(upstream):
    assert upstream.index.declaration_count == 287


def test_c1_follows_18_symlinked_declarations_and_leaves_none_unresolved(upstream):
    assert len(upstream.index.symlink_declarations) == 18
    assert [item.path for item in upstream.index.unresolved] == []


def test_c1_resolves_every_symlink_by_path_arithmetic_to_a_sibling_declaration(upstream):
    """Where the 18 links actually point, which is not where HLD section 4.3.3 says.

    That section reads "many of those links point into the shared directories C3
    excludes". On the pinned tree none of them does: all 18 are relative links to a
    **sibling platform's** declaration, one hop, and the shared directories are the target
    of 406 *other* symlinks under `device/arista/` — plugins, thermal policy, pmon config —
    none of which is a `platform_asic`. The arithmetic is the same either way; the claimed
    connection between C1 and C3 is not there in this tree.
    """
    linked = [item for item in upstream.index.declarations if item.is_symlink]
    assert len(linked) == 18

    for declaration in linked:
        assert declaration.link_hops == 1
        assert declaration.resolved_path != declaration.path
        assert declaration.families
        assert declaration.resolved_path.startswith("device/")
        assert declaration.resolved_path.endswith("/platform_asic")

    assert [item for item in linked if item.resolved_path.startswith(ARISTA_COMMON + "/")] == []


def test_c2_parses_exactly_one_declaration_to_two_families(upstream):
    multi = upstream.index.multi_family
    assert len(multi) == 1
    assert multi[0].path == f"{ARISTA_COMMON}/platform_asic"
    assert multi[0].families == ("broadcom", "broadcom-dnx")


def test_c2_makes_every_declaration_set_valued_not_string_valued(upstream):
    for declaration in upstream.index.declarations:
        assert isinstance(declaration.families, tuple)
        assert len(set(declaration.families)) == len(declaration.families)


def test_c3_excludes_exactly_three_shared_common_directories(upstream):
    assert list(upstream.index.excluded) == [
        "device/arista/x86_64-arista_common",
        "device/broadcom/x86_64-broadcom_common",
        "device/marvell/x86_64-marvell_common",
    ]


def test_c3_leaves_284_declared_platforms(upstream):
    """C3's own contribution, before C6 adds any. 287 declarations less 3 shared directories."""
    declared = [entity for entity in upstream.index.entities if entity.id not in upstream.index.aliases]
    assert len(declared) == 284
    assert upstream.index.declaration_count - len(upstream.index.excluded) == len(declared)


def test_c6_adds_three_aliased_platform_directories(upstream):
    """Rule C6, outbound: an entity directory that is itself a symlink is still a platform.

    Each is a distinct ONIE platform name and a real deployable box running its target's
    data. A tree listing records a symlinked directory as one entry with nothing under it,
    so before this rule all three were invisible — and it is why a filesystem glob, which
    follows directory symlinks, counts three more platforms than a count taken from git.
    """
    assert list(upstream.index.aliases) == [
        "arista/x86_64-arista_7280cr3k_32d4",
        "arista/x86_64-arista_7280cr3k_32p4",
        "barefoot/x86_64-accton_as9516bf_32d-r0",
    ]
    inherited = {alias: upstream.index.by_id(alias).families for alias in upstream.index.aliases}
    assert inherited == {
        "arista/x86_64-arista_7280cr3k_32d4": ("broadcom-dnx",),
        "arista/x86_64-arista_7280cr3k_32p4": ("broadcom-dnx",),
        "barefoot/x86_64-accton_as9516bf_32d-r0": ("barefoot",),
    }


def test_an_aliased_platform_carries_no_declaration_of_its_own(upstream):
    """The trap the coordinator named: 287 platforms and 287 declarations are not the same 287.

    C3 takes three shared directories off the declaration count and C6 puts three aliased
    directories on, and the two happen to be equal. An alias reads its families from the
    directory it points at, so it contributes a platform and never a declaration.
    """
    alias = upstream.index.by_id("arista/x86_64-arista_7280cr3k_32d4")
    assert alias.declaration_path == "device/arista/x86_64-arista_7280cr3_32d4/platform_asic"
    assert alias.directory == "device/arista/x86_64-arista_7280cr3k_32d4"
    assert alias.declaration_path not in {item.path for item in upstream.index.declarations
                                          if item.directory == alias.directory}
    assert upstream.index.declaration_count == 287


def test_the_counting_rules_add_up_to_287_platforms(upstream):
    """The whole chain as one equation: 287 - 3 + 3 = 287, by two unrelated routes."""
    index = upstream.index
    assert index.declaration_count - len(index.excluded) + len(index.aliases) == len(index.entities)
    assert len(index.entities) == 287


def test_c3_excludes_the_arista_common_directory_from_the_platform_set(upstream):
    """First direction of the classifier: a shared library directory is not hardware."""
    assert upstream.index.by_id("arista/x86_64-arista_common") is None


def test_c4_keeps_the_arista_7800_supervisor_which_owns_no_hwsku(upstream):
    """Second direction, and the trap. `owns no HWSKU` is not a synonym for `not a platform`.

    A classifier built on the scan that finds the shared directories would discard this
    chassis supervisor along with them — and 29 more supervisors and fabric cards, the
    parts whose failure takes a whole chassis down.
    """
    supervisor = upstream.index.by_id(ARISTA_7800_SUP)
    assert supervisor is not None
    assert supervisor.owns_hwsku is False
    assert supervisor.families


def test_c6_reverse_reach_puts_a_shared_directorys_contents_back_into_every_platform(upstream):
    """Rule C6, inbound, and the defect it closes. Chains count: two platforms need two hops.

    A change to `device/arista/x86_64-arista_common/pmon_daemon_control.json` is a change
    to the pmon configuration of forty Arista platforms on this tree, and before C6 the
    stage reported zero reached — no platform owns the path, and no platform's
    `platform_asic` resolves through it. Thirty-eight of the forty link at it directly; two
    link at a sibling platform's copy, which links at it in turn.
    """
    reached = upstream.index.reaching(["device/arista/x86_64-arista_common/pmon_daemon_control.json"])
    assert len(reached) == 40
    assert all(item.startswith("arista/") for item in reached)
    assert "arista/x86_64-arista_7060dx4_32" in reached, "reached only by following a two-hop chain"


def test_c4_keeps_30_platforms_that_own_no_hwsku_directory(upstream):
    kept = upstream.index.kept_without_hwsku
    assert len(kept) == 30
    assert ARISTA_7800_SUP in [entity.id for entity in kept]


def test_the_two_directions_of_the_classifier_are_decided_by_different_facts(upstream):
    """Why the `_common` suffix is the rule and `owns no HWSKU` is only the discovery method.

    Both kinds look alike on the test that found them, so the test that found them cannot
    be the test that separates them.
    """
    discovered = [
        item for item in upstream.index.declarations
        if not _owns_hwsku(upstream.index, item.directory) and not _declares_identity(upstream.index, item.directory)
    ]
    names = sorted(item.directory for item in discovered)
    assert names == [
        "device/arista/x86_64-arista_7800_sup",
        "device/arista/x86_64-arista_common",
        "device/broadcom/x86_64-broadcom_common",
        "device/marvell/x86_64-marvell_common",
    ]
    excluded = set(upstream.index.excluded)
    assert sum(1 for name in names if name in excluded) == 3
    assert "device/arista/x86_64-arista_7800_sup" not in excluded


def _owns_hwsku(index, directory: str) -> bool:
    entity = next((item for item in index.entities if item.directory == directory), None)
    return bool(entity and entity.owns_hwsku)


def _declares_identity(index, directory: str) -> bool:
    entity = next((item for item in index.entities if item.directory == directory), None)
    return bool(entity and entity.declares_identity)
