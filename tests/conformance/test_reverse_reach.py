"""Rule C6 against pinned trees: what a change to shared data actually reaches.

The defect these close is the one Scout exists to catch. Commit `3589b565d` edited
`device/arista/x86_64-arista_common/pmon_daemon_control.json` — one file, two lines of
diff — and through inbound git symlinks that file is the pmon configuration of 38 real
Arista platforms. The static stage reported it reached **zero**: no platform owns the path,
because C3 correctly rules the `_common` directory out of the platform set, and no
platform's `platform_asic` resolves through it either. C3 keeps shared directories out of
the *count*; C6 puts their *contents* into every platform's reach.

The other half of C6 is the three entity directories that are themselves symlinks. A tree
listing records one of those as a single entry with nothing underneath, so they were
invisible, and each is a distinct ONIE platform running its target's data.
"""

from scout_impl.models import CHANGE_MODIFIED, FileDiff
from scout_impl.static.coverage import affected_entities

PMON = "device/arista/x86_64-arista_common/pmon_daemon_control.json"

ALIASES = (
    "arista/x86_64-arista_7280cr3k_32d4",
    "arista/x86_64-arista_7280cr3k_32p4",
    "barefoot/x86_64-accton_as9516bf_32d-r0",
)


def test_the_pinned_tree_is_the_commit_that_changed_the_shared_pmon_config(shared_change):
    """The fixture carries the 1,700 links, and deliberately not the file they point at.

    Reverse reach is answered entirely from the link side — the tree listing says which
    entries are symlinks, and their blobs say where each one goes — so the shared file's
    own contents are never needed and are not captured. Worth asserting, because it is the
    property that keeps this cheap: the question is about paths, not about bytes.
    """
    assert shared_change.rev == "3589b565dff1229247826b9094529cf676a89c41"
    assert not shared_change.tree.exists(PMON)
    assert shared_change.index.reaching([PMON])


def test_the_shared_pmon_change_reaches_38_arista_platforms(shared_change):
    """38, not the 36 a single-hop enumeration finds. Two platforms need two hops.

    `x86_64-arista_7060dx4_32` and `x86_64-arista_720dt_48s_mgx` link their
    `pmon_daemon_control.json` at a *sibling platform's* copy, which is itself a link to the
    shared one. Both boxes run the changed bytes, so both are reached; resolving one hop and
    stopping misses them. C6 follows chains under the same hop limit as C1, which is what
    the rule says to do.
    """
    reached = shared_change.index.reaching([PMON])
    assert len(reached) == 38
    assert all(item.startswith("arista/") for item in reached)
    assert "arista/x86_64-arista_7060dx4_32" in reached
    assert "arista/x86_64-arista_720dt_48s_mgx" in reached

    one_hop = {item for item in reached
               if shared_change.tree.read(f"device/{item}/pmon_daemon_control.json").strip().endswith(
                   "x86_64-arista_common/pmon_daemon_control.json")}
    assert len(one_hop) == 36, "the other two are the chained pair"


def test_the_change_set_that_reported_zero_now_reports_38(shared_change):
    """End to end through the query the brief is built from, not just the index helper."""
    changed = [PMON, "dockers/docker-platform-monitor/docker-pmon.supervisord.conf.j2"]
    affected = affected_entities(shared_change.index, changed)
    assert len(affected) == 38


def test_a_path_no_symlink_points_at_reaches_only_its_owner(shared_change):
    """C6 widens reach where links exist and nowhere else; it is not a blanket fan-out."""
    owned = "device/arista/x86_64-arista_7050_qx32/platform_asic"
    assert affected_entities(shared_change.index, [owned]) == ("arista/x86_64-arista_7050_qx32",)


def test_a_path_outside_the_entity_root_reaches_nothing(shared_change):
    assert affected_entities(shared_change.index, ["files/build_templates/init_cfg.json.j2"]) == ()


def test_resolving_the_symlink_graph_costs_one_batched_prefetch(shared_change):
    """NFR-12. 1,700 links hold 449 distinct targets, and the graph asks for them once.

    Sequentially against a blob-filtered remote that is 449 round trips, about three and a
    half minutes at the measured rate; batched it was measured at one trip and half a
    second. The assertion that matters is that the count does not grow with the number of
    links or the number of questions — a regression to one-at-a-time still answers
    correctly and still passes every other test in this file.

    Three batches in a whole run, one per phase: the declarations, the alias directories,
    and the symlink graph. The graph is the third and only happens because this fixture is
    asked a reverse-reach question.
    """
    assert len(shared_change.index.links.links) == 1700
    assert len(set(sha for _, sha in shared_change.index.links.links)) == 449

    assert shared_change.tree.prefetches == 3
    before = shared_change.tree.prefetches
    shared_change.index.reaching([PMON])
    shared_change.index.reaching(["device/arista/x86_64-arista_common/plugins/psu.py"])
    assert shared_change.tree.prefetches == before, "further questions resolve nothing new"


def test_the_reach_is_the_same_question_on_a_later_tree(upstream):
    """Two more platforms had linked at the file by September. The rule does not change."""
    assert len(upstream.index.reaching([PMON])) == 40


def test_the_same_three_directory_aliases_exist_in_both_trees(upstream, fork):
    assert tuple(upstream.index.aliases) == ALIASES
    assert tuple(fork.index.aliases) == ALIASES


def test_an_alias_is_reached_by_a_change_to_the_directory_it_points_at(upstream):
    """The two halves of C6 meet: the alias link's target is an ancestor of the changed path.

    Nothing special-cases this. An aliased directory is a symlink like any other, so a
    change inside its target is a change inside it, and the same reverse reach finds it.
    """
    changed = "device/arista/x86_64-arista_7280cr3_32d4/platform_asic"
    affected = affected_entities(upstream.index, [changed])
    assert "arista/x86_64-arista_7280cr3_32d4" in affected, "the directory that owns the path"
    assert "arista/x86_64-arista_7280cr3k_32d4" in affected, "the alias that shares its data"


def test_an_alias_is_covered_exactly_as_the_platform_it_shadows(upstream):
    """It inherits the declaration, so it inherits the verdict, and it is counted separately."""
    for alias in ALIASES:
        shadow = upstream.index.by_id(alias)
        assert shadow.families
        assert shadow.resolved_via == "symlink"

    uncovered = set(upstream.coverage.uncovered)
    assert set(ALIASES) <= uncovered, "broadcom-dnx and barefoot are built by no job group"


def test_a_hotspot_on_shared_data_carries_the_platforms_it_reaches(shared_change):
    """What a reviewer sees: the fan-out is in the finding, not left for them to work out."""
    from scout_impl.repos import get_adapter
    from scout_impl.static.coverage import query_coverage
    from scout_impl.static.hotspots import rank_hotspots

    adapter = get_adapter("sonic-buildimage")
    changed = [FileDiff(path=PMON, change_type=CHANGE_MODIFIED, path_class=adapter.path_class("platform_data"))]
    coverage = query_coverage(shared_change.index, shared_change.model,
                              affected_entities(shared_change.index, [PMON]))

    hotspot = rank_hotspots(changed, shared_change.index, coverage, len(adapter.path_classes))[0]
    assert hotspot.path == PMON
    assert len([item for item in hotspot.entities if item.startswith("platform:")]) == 38
    assert hotspot.score_breakdown["entity_fanout"] > 0
