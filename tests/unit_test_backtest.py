"""Backtest grading and the per-item fixture round trip it replays from."""

from pathlib import Path

import pytest

from scout_impl.eval.backtest_run import BacktestError, grade_item, run_backtest, summarize, wilson
from scout_impl.eval.fixtures import ItemFixture
from scout_impl.models import MODE_RANGE, ChangeSet, ChangeSetSpec
from scout_impl.static.fixtures import TreeFixture

CAUSE = "c" * 40
PARENT = "p" * 40


def test_an_item_fixture_round_trips_its_tree_entries(tmp_path: Path) -> None:
    tree = TreeFixture.from_files(
        {"azure-pipelines.yml": "stages: []\n", "device/acme/x86_64-acme_1-r0/platform_asic": "broadcom\n"},
        rev=CAUSE,
    )
    change_set = ChangeSet(
        base_sha=PARENT,
        head_sha=CAUSE,
        spec=ChangeSetSpec(base_ref=PARENT, head_ref=CAUSE, mode=MODE_RANGE),
        repo="sonic-net/sonic-buildimage",
    )
    path = tmp_path / "item.json"
    ItemFixture(item_id="incident-0001", cause_sha=CAUSE, parent_sha=PARENT, tree=tree, change_set=change_set).write(
        path
    )

    loaded = ItemFixture.load(path)

    assert {entry.path for entry in loaded.tree.entries} == {entry.path for entry in tree.entries}
    assert loaded.source().read_file(CAUSE, "azure-pipelines.yml") == "stages: []\n"


def _incident(truth: dict) -> dict:
    return {"item_id": "incident-0001", "kind": "incident", "cause_sha": CAUSE, "ground_truth": truth}


def test_an_incident_is_recalled_only_by_the_reading_that_flags_its_platform() -> None:
    item = _incident({"platforms": ["acme/x86_64-acme_1-r0"]})
    coverage = {"affected": ["acme/x86_64-acme_1-r0"], "uncovered": [], "ambiguous": ["acme/x86_64-acme_1-r0"]}

    row = grade_item(item, coverage, {"acme/x86_64-acme_1-r0": ("broadcom",)})

    assert row["string_equality"]["recalled"] is True
    assert row["architecture_aware"]["recalled"] is False


def test_a_control_with_a_never_built_finding_counts_against_the_flag_rate() -> None:
    control = {"item_id": "control-01", "kind": "control", "cause_sha": CAUSE}
    quiet = {"item_id": "control-02", "kind": "control", "cause_sha": CAUSE}
    rows = [
        grade_item(control, {"affected": ["a/b"], "uncovered": ["a/b"]}, {}),
        grade_item(quiet, {"affected": ["a/b"], "covered": ["a/b"]}, {}),
    ]

    metrics = summarize(rows)["architecture_aware"]

    assert metrics["control_flag_rate"]["k"] == 1
    assert metrics["control_flag_rate"]["n"] == 2
    assert metrics["recall"]["n"] == 0


def test_wilson_stays_inside_the_unit_interval() -> None:
    assert wilson(0, 0) is None
    low, high = wilson(0, 20)
    assert low == 0.0 and 0.0 < high < 0.2
    low, high = wilson(20, 20)
    assert 0.8 < low < 1.0 and high == 1.0


def test_offline_without_a_pinned_corpus_says_how_to_capture_one(tmp_path: Path) -> None:
    with pytest.raises(BacktestError, match="--capture"):
        run_backtest(directory=tmp_path / "empty")


def test_the_seed_corpus_is_refused_for_an_adapter_it_does_not_grade(tmp_path: Path) -> None:
    with pytest.raises(BacktestError, match="no corpus"):
        run_backtest(adapter_name="sonic-mgmt", directory=tmp_path)
