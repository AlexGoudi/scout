"""Paths to assess: the feature-group query, and the brief block and comment section it feeds."""

import copy
import json
from pathlib import Path

import pytest

import agent_world as world
from scout_impl.agent.runner import run_agent
from scout_impl.diffparse import parse_diff
from scout_impl.mining.taxonomy import load_taxonomy
from scout_impl.models import ChangeSet, ChangeSetSpec, CommitInfo
from scout_impl.report import build_report, render_comment
from scout_impl.repos import get_adapter
from scout_impl.static.brief import BriefContractError, _check_invariants
from scout_impl.static.engine import analyze, build_brief
from scout_impl.static.fixtures import TreeFixture
from scout_impl.static.related import (
    FEATURE_MAP,
    RULE_NONE,
    RULE_SHARED,
    RULE_UNION,
    FeatureMapError,
    load_feature_map,
)
from scout_impl.static.schema import ValidationError, validate_brief

DEMO_TREE = Path(__file__).resolve().parent / "fixtures" / "demo" / "pmon-24811" / "tree-head.json"
MAP = {
    "bgp": ["a/src/bgpcfgd", "a/rules/frr.mk", "b/tests/bgp"],
    "lldp": ["a/src/lldp", "a/rules/frr.mk", "b/tests/lldp"],
    "acl": ["a/src/acl"],
}


@pytest.fixture
def feature_map(tmp_path):
    path = tmp_path / "map.json"
    path.write_text(json.dumps(MAP))
    return load_feature_map(path)


def test_groups_are_split_by_repository(feature_map):
    assert feature_map.groups["bgp"] == {"a": ("rules/frr.mk", "src/bgpcfgd"), "b": ("tests/bgp",)}


def test_a_path_under_a_prefix_matches_and_a_lookalike_does_not(feature_map):
    assert [hit.id for hit in feature_map.related("a", ["src/bgpcfgd/main.py"]).features] == ["bgp"]
    assert feature_map.related("a", ["src/bgpcfgd-lookalike/main.py"]).rule == RULE_NONE


def test_shared_groups_win_over_the_union(feature_map):
    related = feature_map.related("a", ["rules/frr.mk", "src/bgpcfgd/x.py"])
    assert related.rule == RULE_SHARED
    assert [(hit.id, hit.changed) for hit in related.features] == [("bgp", ("rules/frr.mk", "src/bgpcfgd/x.py"))]
    assert dict(related.paths_to_assess) == {"b": ("tests/bgp",)}


def test_no_shared_group_widens_to_the_union(feature_map):
    related = feature_map.related("a", ["src/bgpcfgd/x.py", "src/acl/y.c"])
    assert related.rule == RULE_UNION
    assert [hit.id for hit in related.features] == ["acl", "bgp"]
    assert dict(related.paths_to_assess) == {"a": ("rules/frr.mk",), "b": ("tests/bgp",)}


def test_a_path_in_no_group_contributes_nothing(feature_map):
    focused = feature_map.related("a", ["src/bgpcfgd/x.py"])
    assert feature_map.related("a", ["src/bgpcfgd/x.py", "README.md"]) == focused
    assert focused.rule == RULE_SHARED


def test_the_changed_paths_and_the_members_they_hit_are_not_listed_again(feature_map):
    related = feature_map.related("a", ["rules/frr.mk"])
    assert [hit.id for hit in related.features] == ["bgp", "lldp"]
    assert dict(related.paths_to_assess) == {"a": ("src/bgpcfgd", "src/lldp"), "b": ("tests/bgp", "tests/lldp")}


def test_the_other_repository_is_queried_by_its_own_prefixes(feature_map):
    related = feature_map.related("b", ["tests/lldp/test_lldp.py"])
    assert dict(related.paths_to_assess) == {"a": ("rules/frr.mk", "src/lldp")}


def test_an_empty_change_is_empty(feature_map):
    assert feature_map.related("a", []).empty


def test_a_bad_feature_map_is_rejected(tmp_path):
    path = tmp_path / "map.json"
    path.write_text("[]")
    with pytest.raises(FeatureMapError):
        load_feature_map(path)
    with pytest.raises(FeatureMapError):
        load_feature_map(tmp_path / "missing.json")


def test_the_taxonomy_hashes_the_same_bytes_the_loader_reads():
    taxonomy = load_taxonomy()
    bgp = next(area for area in taxonomy.features if area.id == "bgp")
    assert bgp.prefixes == load_feature_map().groups["bgp"]["sonic-buildimage"]
    assert load_feature_map().raw == FEATURE_MAP.read_bytes()


BGP_FILE = "src/sonic-bgpcfgd/bgpcfgd/main.py"
BGP_DIFF = f"""diff --git a/{BGP_FILE} b/{BGP_FILE}
index 1111111..2222222 100644
--- a/{BGP_FILE}
+++ b/{BGP_FILE}
@@ -1 +1 @@
-old
+new
"""


def _brief(diff):
    tree = TreeFixture.load(DEMO_TREE)
    commit = CommitInfo(sha=tree.rev, parents=[world.BASE], subject="bgp",
                        files=parse_diff(diff, get_adapter("sonic-buildimage")))
    change_set = ChangeSet(base_sha=world.BASE, head_sha=tree.rev,
                           spec=ChangeSetSpec(base_ref=world.BASE, head_ref=tree.rev),
                           repo="sonic-buildimage", commits=[commit])
    result = analyze(tree.source(), tree.rev, get_adapter("sonic-buildimage"), change_set=change_set)
    return build_brief(result, repo="sonic-net/sonic-buildimage", base_sha=world.BASE, mode="range",
                       run_id="run-1", measured_at="2026-09-29T00:00:00Z")


def test_the_brief_carries_paths_to_assess_for_a_change_in_a_feature_group():
    block = _brief(BGP_DIFF).payload["paths_to_assess"]
    assert block["repo"] == "sonic-buildimage" and block["rule"] == RULE_SHARED
    assert block["features"] == [{"id": "bgp", "changed": [BGP_FILE]}]
    repos = {item["repo"]: item["paths"] for item in block["repos"]}
    assert "src/sonic-bgpcfgd" not in repos["sonic-buildimage"] and "rules/frr.mk" in repos["sonic-buildimage"]
    assert "tests/bgp" in repos["sonic-mgmt"]


def test_a_change_in_no_feature_group_omits_the_block():
    assert "paths_to_assess" not in _brief(world.DIFF.replace("acme", "arista")).payload


def test_the_schema_checks_the_block_and_the_contract_checks_what_it_cannot():
    payload = copy.deepcopy(_brief(BGP_DIFF).payload)
    validate_brief(payload)
    payload["paths_to_assess"]["repos"][0]["paths"].append(BGP_FILE)
    with pytest.raises(BriefContractError):
        _check_invariants(payload)
    payload["paths_to_assess"]["rule"] = "everything"
    with pytest.raises(ValidationError):
        validate_brief(payload)


def test_the_comment_lists_paths_to_assess_only_when_the_brief_has_them():
    brief = world.brief()
    agent = run_agent(brief, world.source(), world.change_set(), provider=None)
    report = build_report(brief, agent, run_id="run-1")
    plain = render_comment(report)
    assert render_comment(report, brief) == plain and "Paths to assess" not in plain

    brief["paths_to_assess"] = {
        "repo": "sonic-buildimage", "rule": RULE_SHARED,
        "features": [{"id": "bgp", "changed": [BGP_FILE]}],
        "repos": [{"repo": "sonic-mgmt", "paths": [f"tests/bgp/t{index}" for index in range(12)]}],
    }
    text = render_comment(report, brief)
    assert "### Paths to assess" in text and "`bgp` (1 changed)" in text
    assert "- `sonic-mgmt` (12): `tests/bgp/t0`" in text and "and 2 more in `scout-brief.json`" in text
    assert text.index("### Paths to assess") < text.index("<sub>Brief")
