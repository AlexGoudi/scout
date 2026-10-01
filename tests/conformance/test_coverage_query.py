"""The coverage-gap query and rule C5, against the pinned upstream tree.

The headline the whole design rests on: 287 platforms, 196 whose ASIC family PR CI builds,
91 it never does under string matching and 80 under the architecture rule. The 91 is a
floor of 74 decided outright plus 17 the rule decides individually — 11 covered, 6 not.

Two figures moved since these tests were first written and both were defects, not drift.
The platform count was 284 because three platforms whose directory is itself a symlink
were invisible (rule C6). And the architecture answer was reported as 71, which counted
every ambiguous platform as covered; six of them are amd64 boxes declaring
`marvell-prestera`, a family built only for arm64 and armhf, so the real figure is 80.
"""

from scout_impl.static.coverage import (
    ARCHITECTURE_AWARE,
    RESOLUTION_COVERED,
    RESOLUTION_UNCOVERED,
    RESOLUTION_UNDETERMINED,
    STRING_EQUALITY,
    entities_declaring,
    query_coverage,
)


def test_the_headline_is_287_platforms_196_built_and_91_never_built(upstream):
    coverage = upstream.coverage
    assert len(coverage.affected) == 287
    assert len(coverage.covered) == 196
    assert coverage.uncovered_under[STRING_EQUALITY] == 91
    assert len(coverage.covered) + coverage.uncovered_under[STRING_EQUALITY] == 287


def test_the_never_built_share_is_32_percent_matching_and_28_percent_architecture(upstream):
    coverage = upstream.coverage
    total = len(coverage.affected)
    assert round(100 * coverage.uncovered_under[STRING_EQUALITY] / total) == 32
    assert round(100 * coverage.uncovered_under[ARCHITECTURE_AWARE] / total) == 28


def test_the_three_sets_are_disjoint_and_exhaustive_over_affected(upstream):
    coverage = upstream.coverage
    assert coverage.is_exhaustive
    assert len(coverage.covered) == 196
    assert len(coverage.uncovered) == 74
    assert len(coverage.ambiguous) == 17


def test_the_ambiguity_resolves_to_91_or_80_and_the_static_stage_publishes_both(upstream):
    assert upstream.coverage.uncovered_under == {STRING_EQUALITY: 91, ARCHITECTURE_AWARE: 80}


def test_the_architecture_rule_decides_each_ambiguous_platform_individually(upstream):
    """The defect this closes. Reading the rule as "ambiguous means covered" gave 74.

    Eleven of the seventeen are arm64 or armhf boxes a qualified job group does build. Six
    are amd64 boxes declaring `marvell-prestera`, and there is no amd64 prestera job at
    all, so those six are never built and the architecture answer is 74 + 6 = 80.
    """
    counts = upstream.coverage.resolution_counts
    assert counts == {RESOLUTION_COVERED: 11, RESOLUTION_UNCOVERED: 6, RESOLUTION_UNDETERMINED: 0}

    not_built = sorted(item for item, answer in upstream.coverage.ambiguous_resolution.items()
                       if answer == RESOLUTION_UNCOVERED)
    assert len(not_built) == 6
    for entity_id in not_built:
        entity = upstream.index.by_id(entity_id)
        assert entity.arch == "amd64"
        assert entity.families == ("marvell-prestera",)
        assert upstream.coverage.ambiguous_job_groups[entity_id] == ()

    assert (upstream.coverage.uncovered_under[ARCHITECTURE_AWARE]
            == len(upstream.coverage.uncovered) + counts[RESOLUTION_UNCOVERED])


def test_the_ambiguous_set_is_exactly_the_marvell_prestera_and_aspeed_platforms(upstream):
    families = sorted({family for names in upstream.coverage.ambiguous_families.values() for family in names})
    assert families == ["aspeed", "marvell-prestera"]
    assert len(entities_declaring(upstream.index, "marvell-prestera")) == 12
    assert len(entities_declaring(upstream.index, "aspeed")) == 5
    assert len(upstream.coverage.ambiguous) == 12 + 5


def test_c5_covers_a_platform_when_any_declared_family_is_built(upstream):
    """Set-valued coverage, not first-value coverage. The dual-family case is the reason."""
    for entity_id in upstream.coverage.covered:
        entity = upstream.index.by_id(entity_id)
        assert set(entity.families) & set(upstream.model.built_families)


def test_no_platform_lands_in_more_than_one_set(upstream):
    coverage = upstream.coverage
    assert set(coverage.covered).isdisjoint(coverage.uncovered)
    assert set(coverage.covered).isdisjoint(coverage.ambiguous)
    assert set(coverage.uncovered).isdisjoint(coverage.ambiguous)


def test_narrowing_to_a_change_set_narrows_the_sets_and_nothing_else(upstream):
    """A pull request's brief and the tree-wide figures come from the same query.

    The three platforms picked are the amd64 `marvell-prestera` boxes, so the architecture
    answer for this slice is 3 rather than 0 — the narrowed query runs the same
    per-platform resolution the whole-tree one does.
    """
    touched = sorted(item for item, answer in upstream.coverage.ambiguous_resolution.items()
                     if answer == RESOLUTION_UNCOVERED)[:3]
    narrowed = query_coverage(upstream.index, upstream.model, touched)
    assert narrowed.affected == tuple(touched)
    assert narrowed.ambiguous == tuple(touched)
    assert narrowed.covered == ()
    assert narrowed.is_exhaustive
    assert narrowed.uncovered_under == {STRING_EQUALITY: 3, ARCHITECTURE_AWARE: 3}


def test_the_fork_gets_the_same_treatment_and_its_own_numbers(fork):
    """A second tree, so the totals are not one tree's accident.

    278 platforms is 277 declarations less 2 shared directories plus the same 3 aliases —
    this fork carries two `_common` directories where upstream has three. 78 are never
    built outright, 88 under string matching, 82 under the architecture rule.
    """
    index, coverage = fork.index, fork.coverage
    assert index.declaration_count == 277
    assert len(index.excluded) == 2
    assert len(index.aliases) == 3
    assert len(index.entities) == 278
    assert index.declaration_count - len(index.excluded) + len(index.aliases) == len(index.entities)

    assert len(coverage.affected) == 278
    assert len(coverage.covered) == 190
    assert len(coverage.uncovered) == 78
    assert len(coverage.ambiguous) == 10
    assert coverage.uncovered_under == {STRING_EQUALITY: 88, ARCHITECTURE_AWARE: 82}
    assert coverage.resolution_counts[RESOLUTION_UNCOVERED] == 4


def test_a_family_distribution_row_from_the_document_still_holds(upstream):
    """Spot-check the largest rows of the family distribution, with C6's aliases folded in.

    `broadcom-dnx` gains two and `barefoot` one against the declaration-only count, because
    all three aliased directories inherit those families. `broadcom` and `mellanox` do not
    move, which is what says the aliases were added where they belong rather than
    everywhere.
    """
    families = upstream.index.families
    assert len(families["broadcom"]) == 155
    assert len(families["mellanox"]) == 36
    assert len(families["broadcom-dnx"]) == 25 + 2
    assert len(families["barefoot"]) == 13 + 1
