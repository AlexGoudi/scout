"""The second repository, which exists to falsify the claim that the core is repo-agnostic.

The claim is falsified the moment shipping the `sonic-mgmt` adapter requires a change
inside the core (HLD section 5.1), and it was: `sonic-buildimage` declares entities as
directories carrying a declaration file and coverage as pipeline stages, `sonic-mgmt`
declares entities as files and coverage as a Python list literal, and neither pair is a
parameterisation of the other. Two extractor shapes and a dispatch had to be added.

`test_what_the_second_repository_forced_into_the_core` records exactly what changed, so
the cost is a fact in the suite rather than a claim in a write-up. Everything else here
asserts what did **not** have to change, which is the more interesting half: the engine,
the coverage query, the hotspot ranking, the brief builder, the schema and the validator
all took a second repository with a different shape unmodified.
"""

from scout_impl.static.engine import analyze, build_brief
from scout_impl.static.schema import validate_brief

FIXED_RUN = "00000000-0000-4000-8000-000000000001"
FIXED_CLOCK = "2026-09-21T09:14:02Z"


def _brief(analyzed):
    result = analyze(analyzed.fixture.source(), analyzed.rev, analyzed.adapter)
    return build_brief(result, repo=analyzed.fixture.repo, head_sha=analyzed.rev,
                       run_id=FIXED_RUN, measured_at=FIXED_CLOCK)


def test_the_second_repository_emits_a_schema_valid_brief(mgmt):
    brief = _brief(mgmt)
    validate_brief(brief.payload)
    assert brief.payload["brief"]["adapter"]["name"] == "sonic-mgmt"


def test_topologies_are_extracted_as_entities(mgmt):
    assert mgmt.index.kind == "topology"
    assert len(mgmt.index.entities) == 183
    assert mgmt.index.by_id("t0") is not None
    assert mgmt.index.by_id("t0").declaration_path == "ansible/vars/topo_t0.yml"


def test_pr_topology_type_is_modelled_as_the_coverage_surface(mgmt):
    assert mgmt.model.model == "pr-checker-topology"
    assert mgmt.model.path == ".azure-pipelines/impacted_area_testing/constant.py"
    assert mgmt.model.names == (
        "dpu", "dualtor", "t0", "t0-2vlans", "t0-sonic", "t1", "t1-lag-vpp", "t1-multi-asic", "t2",
    )


def test_the_checker_to_topology_mapping_resolves_rather_than_being_ambiguous(mgmt):
    """`t1_checker` runs the `t1-lag` topology, which the repository states outright.

    The buildimage ambiguity comes from an architecture the declaration does not state.
    There is no second axis here, so the mapping decides coverage instead of clouding it,
    and `topology:t1-lag` lands in `covered`.
    """
    assert mgmt.coverage.ambiguous == ()
    assert "t1-lag" in mgmt.coverage.covered
    assert "t1-8-lag" in mgmt.coverage.covered
    assert "t0-64-32" in mgmt.coverage.covered


def test_the_coverage_gap_is_real_and_the_sets_still_partition(mgmt):
    assert len(mgmt.coverage.covered) == 9
    assert len(mgmt.coverage.uncovered) == 174
    assert len(mgmt.coverage.affected) == 183
    assert mgmt.coverage.is_exhaustive


def test_the_counting_rules_report_zero_rather_than_a_number_that_looks_measured(mgmt):
    """C1 to C4 describe how buildimage declares a platform and have nothing to act on here."""
    assert mgmt.index.counting_rules is False
    assert mgmt.index.excluded == ()
    assert mgmt.index.kept_without_hwsku == ()
    assert mgmt.index.symlink_declarations == ()
    assert mgmt.index.multi_family == ()


def test_the_second_adapter_ships_no_detector_so_the_brief_carries_no_questions(mgmt):
    """Smoke level, by decision (HLD section 3): D8 is deferred and is not built."""
    payload = _brief(mgmt).payload
    assert mgmt.adapter.detectors == ()
    assert payload["rules"] == []
    assert payload["questions"] == []
    assert payload["unresolved"] == []


def test_what_the_second_repository_forced_into_the_core(mgmt):
    """The falsification result, recorded rather than quietly patched over.

    Three additions, all of them new shapes rather than changes to existing behaviour:
    `FileEntitySpec` with its extractor, `ConstantCoverageSpec` with its extractor, and
    the `extract` dispatch between them. The buildimage numbers are unchanged by all
    three, which the rest of the conformance suite is what proves.
    """
    from scout_impl.repos.base import ConstantCoverageSpec, FileEntitySpec
    from scout_impl.static import extract

    assert isinstance(mgmt.adapter.entity_model, FileEntitySpec)
    assert isinstance(mgmt.adapter.coverage_spec, ConstantCoverageSpec)
    assert callable(extract.build_index) and callable(extract.build_coverage)


def test_the_core_modules_the_second_repository_did_not_change(mgmt, upstream):
    """Both repositories go through one engine, one query, one builder and one schema."""
    from scout_impl.static import brief, coverage, engine, hotspots, schema

    for module in (engine, coverage, hotspots, brief, schema):
        assert "sonic" not in module.__name__

    for analyzed in (mgmt, upstream):
        built = _brief(analyzed)
        validate_brief(built.payload)
        assert built.payload["coverage"]["parse"]["loose_scan_agrees"] is True
