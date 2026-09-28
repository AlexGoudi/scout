import pytest

from conftest import ALICE, BOB, DAY, RepoBuilder, mine_facts
from scout_impl.dataset.history import CommitFacts
from scout_impl.dataset.labels import BlameCache, _meaningful, commit_labels, link_reverts, szz


def fact(index, *, sha=None, landed=None, pr=None, revert=False, reverts_sha=None, reverts_pr=None, merge=False,
         submodule_only=False, fix=False, author="a", areas=("component:x",), changes=(("M", "f", "f"),)):
    return CommitFacts(
        index=index,
        sha=sha or f"{index:040x}",
        parent=None if index == 0 else f"{index - 1:040x}",
        landed=landed if landed is not None else index * DAY,
        author_id=author,
        is_merge=merge,
        pr_number=pr,
        is_revert=revert,
        reverts_sha=reverts_sha,
        reverts_pr=reverts_pr,
        fix_like=fix,
        submodule_only=submodule_only,
        areas=tuple(areas),
        changes=tuple(changes),
        szz_targets=(),
        szz_deleted_lines=0,
        churn=1,
    )


def test_reverts_link_by_sha_then_pr_and_the_earliest_revert_wins():
    facts = [
        fact(0, pr=10),
        fact(1, pr=11),
        fact(2, revert=True, reverts_sha=f"{0:040x}", reverts_pr=99),
        fact(3, revert=True, reverts_pr=11),
        fact(4, revert=True, reverts_pr=11),
        fact(5, revert=True, reverts_pr=12),
        fact(6, pr=12),
    ]
    links, stats = link_reverts(facts)
    assert {target: (link.revert, link.method) for target, link in links.items()} == {0: (2, "sha"), 1: (3, "pr")}
    assert stats == {"reverts": 4, "linked_by_sha": 1, "linked_by_pr": 2, "unlinked": 1, "nested": 0}


def test_a_revert_of_a_revert_is_flagged_nested():
    facts = [fact(0, pr=1), fact(1, pr=2, revert=True, reverts_pr=1), fact(2, revert=True, reverts_pr=2)]
    links, stats = link_reverts(facts)
    assert links[1].nested and not links[0].nested and stats["nested"] == 1


def test_revert_labels_windows_and_nulls():
    facts = [
        fact(0, pr=1),
        fact(1, merge=True),
        fact(2, submodule_only=True),
        fact(20, pr=None, revert=True, reverts_pr=1, landed=20 * DAY),
    ]
    facts = [CommitFacts(**{**item.__dict__, "index": position}) for position, item in enumerate(facts)]
    links, _ = link_reverts(facts)
    rows = commit_labels(facts, links, bugs={})
    assert rows[0]["reverted"] and rows[0]["reverted_by"] == facts[3].sha
    assert (rows[0]["reverted_within_7d"], rows[0]["reverted_within_30d"], rows[0]["revert_lead_days"]) == (
        False,
        True,
        20.0,
    )
    assert rows[1]["reverted"] is None and rows[1]["bug_introducing"] is None
    assert rows[2]["reverted"] is False and rows[2]["bug_introducing"] is None
    assert rows[3]["bug_introducing"] is False
    assert commit_labels(facts, links, bugs=None)[0]["bug_introducing"] is None


@pytest.mark.parametrize(
    "line, expected",
    [("", False), ("   }", False), ("});", False), ("// comment", False), ("# comment", False),
     ("#include <x.h>", True), ("x = 1", True), ("* item", False), ("return y;", True)],
)
def test_trivial_lines_are_not_blamed(line, expected):
    assert _meaningful(line) is expected


def szz_history(path):
    repo = RepoBuilder(path)
    repo.write("src/x.py", "def f():\n    return 1\n")
    shas = {"base": repo.commit("Add x (#1)")}
    repo.write("src/x.py", "def f():\n    return 1 / 0\n\n# note\n")
    shas["introducer"] = repo.commit("Tune f (#2)", identity=BOB)
    repo.write("src/x.py", "def f():\n    return 1 / 0\n\n# note\ndef g():\n    return 2\n")
    shas["bystander"] = repo.commit("Add g (#3)")
    repo.write("src/x.py", "def f():\n    return 1\n# note\ndef g():\n    return 2\n")
    repo.write("docs/x.md", "old\n")
    shas["fix"] = repo.commit("Fix crash in f (#4)", identity=ALICE)
    return repo, shas


def test_szz_blames_the_changed_lines_at_the_parent(tmp_path):
    repo, shas = szz_history(tmp_path / "repo")
    git, _, facts = mine_facts(repo.path)
    index = {fact.sha: fact.index for fact in facts}
    bugs, stats = szz(git, facts, cache=BlameCache(tmp_path / "cache"), workers=2)
    assert set(bugs) == {index[shas["introducer"]]}
    link = bugs[index[shas["introducer"]]]
    assert (link.fix, link.fix_count) == (index[shas["fix"]], 1)
    assert stats["fix_commits"] == 1 and stats["blame_calls"] == 1
    rows = commit_labels(facts, {}, bugs)
    assert rows[index[shas["introducer"]]]["fixed_by"] == shas["fix"]
    assert rows[index[shas["introducer"]]]["fix_lead_days"] == 2.0
    assert rows[index[shas["bystander"]]]["bug_introducing"] is False


def test_szz_reads_the_blame_cache(tmp_path, monkeypatch):
    repo, shas = szz_history(tmp_path / "repo")
    git, _, facts = mine_facts(repo.path)
    cache = BlameCache(tmp_path / "cache")
    first, _ = szz(git, facts, cache=cache)

    def refuse(*args, **kwargs):
        raise AssertionError("blame should come from the cache")

    monkeypatch.setattr(git, "blame", refuse)
    assert szz(git, facts, cache=cache)[0] == first


def test_oversized_fixes_are_skipped(tmp_path, monkeypatch):
    from scout_impl.dataset import labels

    repo, _ = szz_history(tmp_path / "repo")
    git, _, facts = mine_facts(repo.path)
    monkeypatch.setattr(labels, "SZZ_MAX_DELETED_LINES", 0)
    bugs, stats = szz(git, facts, cache=BlameCache(None))
    assert bugs == {} and stats["fix_commits_too_large"] == 1
