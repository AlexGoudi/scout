"""The code-enforced constraints, one at a time, against the world's brief and trees."""

import pytest

import agent_world as world
from scout_impl.agent.assemble import Group
from scout_impl.agent.budget import Budget
from scout_impl.agent.checks import (
    OUTCOME_CONFIRMED,
    OUTCOME_CONTESTED,
    OUTCOME_UNCONFIRMED,
    CitationResolver,
    ClosedWorld,
    cited_job_group,
    entity_closure,
    rule_consistency,
)
from scout_impl.agent.evidence import EvidenceBook, add_link, add_listing
from scout_impl.agent.protocol import AnswerItem
from scout_impl.agent.toolbox import Toolbox
from scout_impl.core.runlog import RunLog
from scout_impl.static.fixtures import TreeFixture

ARM64 = Group(id="B", members=(world.PRE_ARM64,), representative=world.PRE_ARM64, arch="arm64",
              families=("marvell-prestera",), rule_resolution="covered", rule_job_groups=("marvell-prestera-arm64",))
AMD64 = Group(id="A", members=(world.PRE_AMD64,), representative=world.PRE_AMD64, arch="amd64",
              families=("marvell-prestera",), rule_resolution="uncovered")


def _world(shown=()):
    return ClosedWorld(world.brief()["entities"], shown)


def _answer(covered=True, job_group="marvell-prestera-arm64", reason="", cite=("E1",)):
    return AnswerItem(group="B", reason=reason, cite=tuple(cite), covered=covered, job_group=job_group)


def _toolbox():
    payload = world.brief()
    budget = Budget(payload["budget"], 2)
    return Toolbox(world.source(), world.HEAD, world.BASE, payload["entities"], budget, RunLog())


# --- entity closure ------------------------------------------------------------------------


def test_names_from_the_brief_pass_closure():
    answer = _answer(reason="arm64-acme_pre-r0 is built by marvell-prestera-arm64")
    assert entity_closure(answer, _world()) is None


def test_an_invented_job_group_is_outside_the_closed_world():
    problem = entity_closure(_answer(job_group="marvell-prestera-amd64"), _world())
    assert "marvell-prestera-amd64" in problem


def test_an_invented_platform_in_the_reason_is_outside_the_closed_world():
    problem = entity_closure(_answer(reason="like x86_64-acme_fake-r0, it is covered"), _world())
    assert "x86_64-acme_fake-r0" in problem


def test_a_job_group_shaped_name_in_the_reason_is_checked_too():
    problem = entity_closure(_answer(covered=False, job_group="", reason="only broadcom-amd64 would build it"),
                             _world())
    assert "broadcom-amd64" in problem


def test_a_shared_directory_the_evidence_showed_is_not_an_invention():
    """`x86_64-acme_common` is no entity, but the cause the model was shown lives there."""
    answer = _answer(covered=False, job_group="", reason="x86_64-acme_common is linked by both")
    assert entity_closure(answer, _world()) is not None
    assert entity_closure(answer, _world(shown=[world.PMON])) is None


def test_ordinary_words_with_dashes_are_not_entities():
    answer = _answer(covered=False, job_group="", reason="a pass-through, read-only, non-arm64 change")
    assert entity_closure(answer, _world()) is None


# --- cited job group -----------------------------------------------------------------------


def test_a_covered_claim_through_the_matching_group_passes():
    assert cited_job_group(_answer(), ARM64, _world()) is None


def test_a_covered_claim_through_a_group_of_the_wrong_architecture_is_refused():
    problem = cited_job_group(_answer(job_group="marvell-prestera-armhf"), ARM64, _world())
    assert "armhf" in problem and "arm64" in problem


def test_a_covered_claim_through_a_group_of_the_wrong_family_is_refused():
    """A measured 7B failure: 'aspeed-arm64 builds armhf platforms, matching marvell-prestera-armhf'."""
    problem = cited_job_group(_answer(job_group="broadcom"), ARM64, _world())
    assert "broadcom" in problem and "do not declare" in problem


def test_a_covered_claim_naming_no_group_is_refused():
    assert "names no job group" in cited_job_group(_answer(job_group=""), ARM64, _world())


def test_an_uncovered_claim_needs_no_group():
    assert cited_job_group(_answer(covered=False, job_group=""), AMD64, _world()) is None


# --- rule consistency ----------------------------------------------------------------------


def test_agreeing_with_the_rule_confirms_it():
    assert rule_consistency(_answer(), ARM64) == (OUTCOME_CONFIRMED, "")
    assert rule_consistency(_answer(covered=False, job_group=""), AMD64)[0] == OUTCOME_CONFIRMED


def test_disagreeing_with_the_rule_is_contested_and_shows_both_answers():
    outcome, detail = rule_consistency(_answer(covered=False, job_group=""), ARM64)
    assert outcome == OUTCOME_CONTESTED
    assert "the rule says covered by marvell-prestera-arm64" in detail
    assert "the model says not covered" in detail


def test_a_group_the_rule_could_not_answer_can_only_be_unconfirmed():
    undetermined = Group(id="X", members=(), representative="", rule_resolution="undetermined")
    assert rule_consistency(_answer(covered=False), undetermined)[0] == OUTCOME_UNCONFIRMED


# --- the citation resolver -----------------------------------------------------------------


def test_a_quote_that_reads_as_the_tree_says_resolves():
    toolbox = _toolbox()
    book = EvidenceBook()
    book.add("affected", "device/acme/x86_64-acme_dnx-r0/platform_asic", "head", 1, 1, "broadcom-dnx")
    book.add("cause", world.PMON, "base", 2, 2, '    "skip_fancontrol": true')
    evidence, problem = CitationResolver(toolbox, book).resolve(["E1", "E2"])
    assert problem is None
    assert [item.id for item in evidence] == ["E1", "E2"]


def test_a_quoted_run_ending_in_a_blank_line_resolves():
    """Found live on #24811: added lines ending in a blank line were refused, because the quote
    was joined with newlines and split with `splitlines`, which drops the trailing blank."""
    fixture = TreeFixture.from_files({"t.j2": "{% if x %}\n  y\n{% endif %}\n\nz\n"}, rev=world.HEAD)
    payload = world.brief()
    toolbox = Toolbox(fixture.source(), world.HEAD, "", payload["entities"], Budget(payload["budget"], 2), RunLog())
    book = EvidenceBook()
    book.add("cause", "t.j2", "head", 1, 4, "\n".join(["{% if x %}", "  y", "{% endif %}", ""]))
    assert CitationResolver(toolbox, book).resolve(["E1"])[1] is None


def test_a_quote_that_no_longer_matches_drops_the_answer():
    book = EvidenceBook()
    book.add("cause", world.PMON, "head", 2, 2, '    "skip_fancontrol": false,')
    evidence, problem = CitationResolver(_toolbox(), book).resolve(["E1"])
    assert evidence == []
    assert "does not read as quoted" in problem


def test_a_quote_at_the_wrong_revision_drops_the_answer():
    book = EvidenceBook()
    book.add("cause", world.PMON, "base", 3, 3, '    "xcvrd": {')
    _, problem = CitationResolver(_toolbox(), book).resolve(["E1"])
    assert problem is not None


def test_citing_an_id_that_was_never_shown_drops_the_answer():
    book = EvidenceBook()
    book.add("affected", "device/acme/x86_64-acme_dnx-r0/platform_asic", "head", 1, 1, "broadcom-dnx")
    _, problem = CitationResolver(_toolbox(), book).resolve(["E1", "E7"])
    assert "E7" in problem


def test_citing_nothing_is_unresolvable():
    assert CitationResolver(_toolbox(), EvidenceBook()).resolve([])[1] == "cites no evidence"


def test_a_symlink_resolves_by_re_reading_its_target():
    toolbox = _toolbox()
    book = EvidenceBook()
    link = "device/acme/x86_64-acme_dnx-r0/pmon_daemon_control.json"
    add_link(book, link, world.LINK_TARGET)
    add_link(book, link.replace("dnx-r0", "bcm-r0"), "../elsewhere.json")
    resolver = CitationResolver(toolbox, book)
    assert resolver.resolve(["E1"])[1] is None
    assert "now links to" in resolver.resolve(["E2"])[1]


def test_a_listing_resolves_by_re_listing():
    toolbox = _toolbox()
    entries, total = toolbox.list_tree("device/acme/x86_64-acme_dnx-r0")
    book = EvidenceBook()
    add_listing(book, "device/acme/x86_64-acme_dnx-r0", "head", entries, total)
    assert CitationResolver(toolbox, book).resolve(["E1"])[1] is None


def test_re_reads_are_charged_like_any_read_and_verified_once():
    toolbox = _toolbox()
    book = EvidenceBook()
    book.add("affected", "device/acme/x86_64-acme_dnx-r0/platform_asic", "head", 1, 1, "broadcom-dnx")
    resolver = CitationResolver(toolbox, book)
    resolver.resolve(["E1"])
    resolver.resolve(["E1"])
    assert toolbox.blob_reads == 1


@pytest.mark.parametrize("line_start,line_end", [(0, 1), (2, 1), (7, 9)])
def test_a_line_range_the_file_does_not_have_is_unresolvable(line_start, line_end):
    book = EvidenceBook()
    book.add("affected", "device/acme/x86_64-acme_dnx-r0/platform_asic", "head", line_start, line_end, "x")
    assert "line(s)" in CitationResolver(_toolbox(), book).resolve(["E1"])[1]
