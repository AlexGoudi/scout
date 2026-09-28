"""The risk brief as a contract: schema-valid, self-consistent, and byte-identical.

Three kinds of assertion. The schema catches shape. `_check_invariants` in the builder
catches the contracts the schema cannot express — components summing to their score, the
three sets partitioning `affected`, the closed world actually being closed. And the
byte-identical test is NFR-3 for the static stage: given a pinned tree, a fixed run id and
a fixed clock, the brief is a pure function of the tree and the adapter's rule pack, so
two runs produce the same bytes or something is carrying state it should not.
"""

import json

import pytest

from scout_impl.static.brief import BriefContractError
from scout_impl.static.engine import analyze, build_brief
from scout_impl.static.schema import ValidationError, brief_schema, validate_brief

FIXED_RUN = "00000000-0000-4000-8000-000000000000"
FIXED_CLOCK = "2026-09-21T09:14:02Z"


def _brief(analyzed):
    result = analyze(analyzed.fixture.source(), analyzed.rev, analyzed.adapter)
    return build_brief(result, repo=analyzed.fixture.repo, head_sha=analyzed.rev,
                       run_id=FIXED_RUN, measured_at=FIXED_CLOCK)


def test_the_brief_validates_against_the_versioned_schema_on_write(upstream):
    brief = _brief(upstream)
    validate_brief(brief.payload)
    assert brief.payload["schema_version"] == "1.0"


def test_the_brief_is_byte_identical_across_runs_over_a_pinned_tree(upstream):
    """NFR-3 for the static stage, with the one field that cannot be a pure function named.

    `static_duration_s` is a wall-clock measurement and differs by microseconds between
    runs. It is the only such field once the run id and the clock are supplied, and the
    test says so explicitly rather than comparing a filtered document and calling the
    whole brief deterministic.
    """
    first, second = _brief(upstream), _brief(upstream)
    assert first.canonical_json() == second.canonical_json()
    assert first.sha() == second.sha()

    canonical = json.loads(first.canonical_json())
    assert "static_duration_s" not in canonical["brief"]
    assert canonical["brief"]["blobs_read"] == 34, "a measurement that *is* a function of the tree stays"

    differing = {
        key for key in first.payload["brief"]
        if first.payload["brief"][key] != second.payload["brief"][key]
    }
    assert differing <= {"static_duration_s"}


def test_the_measurement_fields_report_what_the_run_actually_cost(upstream):
    """34 reads, not 31: rule C6 resolves the three aliased directories, and those cost.

    The 1,853 inbound symlinks cost nothing here, because a whole-tree brief asks no
    reverse-reach question and the symlink graph resolves lazily.
    """
    payload = _brief(upstream).payload["brief"]
    assert payload["tree_paths"] == 20155
    assert payload["static_duration_s"] > 0
    assert payload["blobs_read"] == 34
    assert payload["blob_cache_hits"] == 287
    assert payload["status"] == "complete"


def test_the_counting_rules_are_auditable_in_the_artifact(upstream):
    """287 less three shared directories plus three aliased ones is 287, shown as an equation.

    Both adjustments are published separately and on purpose. The declaration count and the
    platform count are both 287 on this tree for entirely unrelated reasons, and a brief
    that printed only the total would read as though they were the same quantity.
    """
    coverage = _brief(upstream).payload["coverage"]
    assert coverage["declarations_in_tree"] == 287
    assert coverage["platforms_in_tree"] == 287
    assert coverage["excluded_as_non_platform"] == [
        "device/arista/x86_64-arista_common",
        "device/broadcom/x86_64-broadcom_common",
        "device/marvell/x86_64-marvell_common",
    ]
    assert coverage["aliased_platforms"] == 3
    assert coverage["aliased_as_platform"] == [
        "arista/x86_64-arista_7280cr3k_32d4",
        "arista/x86_64-arista_7280cr3k_32p4",
        "barefoot/x86_64-accton_as9516bf_32d-r0",
    ]
    assert (coverage["declarations_in_tree"] - len(coverage["excluded_as_non_platform"])
            + coverage["aliased_platforms"] == coverage["platforms_in_tree"])
    assert coverage["kept_without_hwsku"] == 30
    assert coverage["symlinked_declarations"] == 18
    assert coverage["unresolved_declarations"] == 0
    assert coverage["multi_family_declarations"] == 1


def test_the_parse_and_its_cross_check_reach_the_brief(upstream):
    coverage = _brief(upstream).payload["coverage"]
    assert coverage["job_groups"] == 9
    assert coverage["job_group_names"] == [
        "alpinevs", "aspeed-arm64", "broadcom", "marvell-prestera-arm64",
        "marvell-prestera-armhf", "mellanox", "nvidia-bluefield", "vpp", "vs",
    ]
    assert coverage["parse"]["loose_scan_agrees"] is True
    assert coverage["parse"]["scope"] == "Build+BuildVS"


def test_the_coverage_sets_reach_the_brief_as_entity_ids(upstream):
    coverage = _brief(upstream).payload["coverage"]
    assert len(coverage["affected"]) == 287
    assert len(coverage["covered"]) == 196
    assert len(coverage["uncovered"]) == 74
    assert len(coverage["ambiguous"]) == 17
    assert all(item.startswith("platform:") for item in coverage["affected"])


def test_the_unresolved_block_carries_both_candidate_answers(upstream):
    unresolved = _brief(upstream).payload["unresolved"]
    assert len(unresolved) == 1
    item = unresolved[0]
    assert item["id"] == "u-001"
    assert item["kind"] == "family_name_arity"
    assert item["adjudicated_by"] == "agent"
    assert {candidate["rule"]: candidate["uncovered"] for candidate in item["candidates"]} == {
        "string-equality": 91,
        "architecture-aware": 80,
    }


def test_the_candidate_total_and_its_per_platform_working_are_one_computation(upstream):
    """The defect this closes: the two used to be computed separately and differ by six.

    `candidates` reported 71 for the architecture rule — every ambiguous platform counted
    as covered — while the `rule_candidate` in the same brief correctly totalled 77 over
    its own per-platform answers. Both now read the one resolution, and the brief builder
    rejects a brief where they disagree, so they cannot drift apart again.
    """
    item = _brief(upstream).payload["unresolved"][0]
    candidate = item["rule_candidate"]
    headline = next(entry for entry in item["candidates"] if entry["rule"] == candidate["candidate"])

    assert candidate["candidate"] == "architecture-aware"
    assert headline["uncovered"] == candidate["uncovered"] == 80

    answers = [platform["resolution"] for platform in candidate["platforms"]]
    assert len(answers) == 17
    assert answers.count("covered") == 11
    assert answers.count("uncovered") == 6
    assert answers.count("undetermined") == 0
    assert len(_brief(upstream).payload["coverage"]["uncovered"]) + answers.count("uncovered") == 80


def test_every_rule_carries_a_citation_into_the_tree(upstream):
    rules = _brief(upstream).payload["rules"]
    # BI-R3 and BI-R4 are stated because this tree has an ambiguous set to adjudicate.
    assert [rule["id"] for rule in rules] == ["BI-R1", "BI-R2", "BI-R3", "BI-R4"]
    for rule in rules:
        assert rule["citations"], f"{rule['id']} has no citation, which is an adapter bug"
        for citation in rule["citations"]:
            assert citation["path"]
            assert citation["line_start"] >= 1
            assert citation["line_end"] >= citation["line_start"]


def test_the_rule_citations_resolve_to_real_lines_in_the_pinned_tree(upstream):
    for rule in _brief(upstream).payload["rules"]:
        for citation in rule["citations"]:
            lines = upstream.tree.read(citation["path"]).splitlines()
            assert citation["line_end"] <= len(lines)


def test_the_entity_closure_is_closed(upstream):
    payload = _brief(upstream).payload
    assert payload["entity_closure"] is True
    known = {entity["id"] for entity in payload["entities"]}
    for block in ("hotspots", "questions", "unresolved"):
        for item in payload[block]:
            assert set(item["entities"]) <= known
    for key in ("affected", "covered", "uncovered", "ambiguous"):
        assert set(payload["coverage"][key]) <= known


def test_the_closed_world_names_the_job_groups_and_the_families(upstream):
    entities = {entity["id"]: entity for entity in _brief(upstream).payload["entities"]}
    assert "ci_job_group:marvell-prestera-arm64" in entities
    assert entities["ci_job_group:marvell-prestera-arm64"]["arch"] == "arm64"
    assert entities["asic_family:marvell-prestera"]["members"] == 12
    assert entities["asic_family:broadcom"]["members"] == 155
    assert entities["platform:arista/x86_64-arista_7800_sup"]["kind"] == "platform"


def test_a_question_may_only_name_a_rule_the_brief_states(upstream):
    payload = _brief(upstream).payload
    stated = {rule["id"] for rule in payload["rules"]}
    assert payload["questions"]
    for question in payload["questions"]:
        assert question["rule"] in stated


def test_the_builder_rejects_a_brief_whose_coverage_sets_overlap(upstream):
    """The contract the schema cannot express, checked before the artifact lands."""
    brief = _brief(upstream)
    broken = json.loads(brief.to_json())
    broken["coverage"]["uncovered"].append(broken["coverage"]["covered"][0])

    from scout_impl.static.brief import _check_invariants
    with pytest.raises(BriefContractError) as raised:
        _check_invariants(broken)
    assert "overlap" in str(raised.value)


def test_the_validator_rejects_a_brief_that_loses_a_required_field(upstream):
    broken = json.loads(_brief(upstream).to_json())
    del broken["coverage"]["parse"]
    with pytest.raises(ValidationError) as raised:
        validate_brief(broken)
    assert "parse" in str(raised.value)


def test_the_schema_is_the_one_the_hld_defines(upstream):
    """Field names, not shape: a renamed field is a broken contract with stage 2."""
    schema = brief_schema()
    assert set(schema["required"]) == {
        "schema_version", "brief", "hotspots", "entities", "coverage",
        "rules", "questions", "unresolved", "entity_closure", "budget",
    }
    coverage = schema["$defs"]["coverage"]["properties"]
    for field in ("job_group_names", "parse", "declarations_in_tree", "platforms_in_tree",
                  "excluded_as_non_platform", "kept_without_hwsku", "affected", "covered",
                  "uncovered", "ambiguous"):
        assert field in coverage
    run = schema["$defs"]["run"]["properties"]
    assert "static_duration_s" in run and "blobs_read" in run
