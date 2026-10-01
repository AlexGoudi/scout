"""Fixtures for the conformance suite: pinned trees, analyzed offline.

Every test in this directory runs against a committed tree fixture and calls no model and
no network. That is the whole point — the conformance suite is what replaces a
verification stage for the committed detector, so it has to be cheap
enough to run on every change and hermetic enough to be believed.

The fixtures are session-scoped because analyzing one costs about 80 ms and there is no
state to leak: a `FixtureSource` is immutable and a `TreeIndex` over it is a read cache.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from scout_impl.repos import RepoAdapter, get_adapter
from scout_impl.static.coverage import CoverageResult, query_coverage
from scout_impl.static.extract import build_coverage, build_index
from scout_impl.static.fixtures import TreeFixture, load_fixture
from scout_impl.static.pipeline import CoverageModel
from scout_impl.static.platforms import EntityIndex
from scout_impl.static.treeindex import TreeIndex

TREES = Path(__file__).resolve().parents[1] / "fixtures" / "trees"

UPSTREAM = "sonic-buildimage-master-62cfe50.json"
FORK = "nokia-fork-202605.json"
MGMT = "sonic-mgmt-8355f75.json"
SHARED_CHANGE = "sonic-buildimage-3589b56.json"


@dataclass(frozen=True)
class Analyzed:
    """One pinned tree, with everything the static stage established over it."""

    fixture: TreeFixture
    adapter: RepoAdapter
    tree: TreeIndex
    index: EntityIndex
    model: CoverageModel
    coverage: CoverageResult

    @property
    def rev(self) -> str:
        return self.fixture.rev


def analyze_fixture(name: str, adapter_name: Optional[str] = None) -> Analyzed:
    fixture = load_fixture(TREES / name)
    adapter = get_adapter(adapter_name or fixture.adapter)
    tree = TreeIndex(fixture.source(), fixture.rev)
    index = build_index(tree, adapter.entity_model)
    model = build_coverage(tree, adapter.coverage_spec)
    return Analyzed(
        fixture=fixture,
        adapter=adapter,
        tree=tree,
        index=index,
        model=model,
        coverage=query_coverage(index, model),
    )


@pytest.fixture(scope="session")
def upstream() -> Analyzed:
    """Live `sonic-net/sonic-buildimage` master as of 21 Sep 2026, pinned at 62cfe5086."""
    return analyze_fixture(UPSTREAM)


@pytest.fixture(scope="session")
def fork() -> Analyzed:
    """The Nokia downstream fork: a second tree, and the one the Build-only scope is run on.

    It is not a parser-defect exhibit. The "5 job groups against 8" once attributed to this
    tree was two hand measurements taken with different stage patterns, not two parses
    disagreeing; this parser reads 8 on it and its strict and loose parses agree.
    """
    return analyze_fixture(FORK)


@pytest.fixture(scope="session")
def shared_change() -> Analyzed:
    """Upstream at `3589b565d`, the commit that edited a shared Arista pmon config.

    Pinned because that change is the clearest instance of what rule C6 exists for: one
    file, dozens of boxes, and nothing in the path itself to say so. Reached through the
    Nokia clone, which mirrors upstream commits under the same sha.
    """
    return analyze_fixture(SHARED_CHANGE)


@pytest.fixture(scope="session")
def mgmt() -> Analyzed:
    """The second repository, present only to falsify the repo-agnostic claim."""
    return analyze_fixture(MGMT)
