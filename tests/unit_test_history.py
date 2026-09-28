import pytest

from conftest import ALICE, BOB, DAY, RepoBuilder
from scout_impl.dataset.build import build_dataset
from scout_impl.dataset.categories import categorize
from scout_impl.dataset.history import YEAR, history_features
from scout_impl.dataset.labels import link_reverts
from scout_impl.dataset.shards import iter_json
from unit_test_labels import fact


def test_author_experience_and_recency_weighting():
    facts = [
        fact(0, landed=0, author="a"),
        fact(1, landed=2 * YEAR + DAY, author="a"),
        fact(2, landed=2 * YEAR + 2 * DAY, author="b"),
        fact(3, landed=2 * YEAR + 3 * DAY, author="a", areas=("component:y",)),
    ]
    rows = history_features(facts, {})
    assert [row["author_prior_commits"] for row in rows] == [0, 1, 0, 2]
    assert [row["author_first_commit"] for row in rows] == [True, False, True, False]
    assert rows[1]["author_recent_experience"] == pytest.approx(1 / 3, abs=1e-6)
    assert rows[3]["author_recent_experience"] == pytest.approx(1 + 1 / 3, abs=1e-6)
    assert rows[3]["author_area_experience"] == 0 and rows[1]["author_area_experience"] == 1


def test_file_history_follows_renames_and_resets_after_deletion():
    facts = [
        fact(0, author="a", changes=[("A", None, "old.py")]),
        fact(1, author="b", fix=True, changes=[("M", "old.py", "old.py")]),
        fact(2, author="a", changes=[("R", "old.py", "new.py")]),
        fact(3, author="c", changes=[("M", "new.py", "new.py"), ("A", None, "other.py")]),
        fact(4, author="c", changes=[("D", "new.py", None)]),
        fact(5, author="c", changes=[("A", None, "new.py")]),
    ]
    rows = history_features(facts, {})
    assert rows[3]["file_prior_changes"] == 3
    assert rows[3]["file_prior_authors"] == 2
    assert rows[3]["file_prior_fix_touches"] == 1
    assert rows[3]["file_days_since_last_change"] == 1.0
    assert rows[3]["file_new_share"] == 0.5
    assert rows[5]["file_prior_changes"] == 0 and rows[5]["file_days_since_last_change"] is None


def test_area_revert_rate_counts_only_reverts_that_already_landed():
    facts = [
        fact(0, pr=1),
        fact(1),
        fact(2, revert=True, reverts_pr=1),
        fact(3),
    ]
    links, _ = link_reverts(facts)
    rows = history_features(facts, {target: link.revert for target, link in links.items()})
    assert rows[2]["area_revert_rate"] == 0.0
    assert rows[3]["area_revert_rate"] == pytest.approx(1 / 3)


def test_burst_counts_area_commits_in_the_last_seven_days():
    facts = [fact(index, landed=index * DAY) for index in range(10)]
    rows = history_features(facts, {})
    assert [row["area_commits_last_7d"] for row in rows][:3] == [0, 1, 2]
    assert rows[9]["area_commits_last_7d"] == 7


def test_history_of_a_prefix_equals_the_prefix_of_the_history():
    facts = [
        fact(0, pr=1, author="a", changes=[("A", None, "a.py")]),
        fact(1, author="b", fix=True, changes=[("M", "a.py", "a.py")]),
        fact(2, revert=True, reverts_pr=1, author="a", changes=[("M", "a.py", "a.py")]),
        fact(3, author="b", changes=[("R", "a.py", "b.py")], areas=("component:y", "feature:bgp")),
        fact(4, author="c", changes=[("M", "b.py", "b.py")], areas=("feature:bgp",)),
    ]

    def features(prefix):
        links, _ = link_reverts(prefix)
        return history_features(prefix, {target: link.revert for target, link in links.items()})

    full = features(facts)
    for end in range(1, len(facts) + 1):
        assert features(facts[:end]) == full[:end]


def lookahead_history(path):
    repo = RepoBuilder(path)
    repo.write("src/a.py", "a = 1\n")
    shas = [repo.commit("Add a (#1)")]
    repo.write("src/a.py", "a = 2\n")
    shas.append(repo.commit("Fix a (#2)", identity=BOB))
    repo.write("dockers/docker-fpm-frr/x.j2", "x\n")
    shas.append(repo.commit("[bgp] add template (#3)"))
    shas.append(repo.revert(shas[1]))
    repo.write("src/a.py", "a = 3\nb = 1\n")
    shas.append(repo.commit("Tune a (#5)", identity=ALICE))
    return repo, shas


def test_dataset_built_up_to_a_commit_gives_it_the_same_history(tmp_path):
    repo, shas = lookahead_history(tmp_path / "repo")
    full = list(iter_json(_build(repo, "HEAD", tmp_path / "full") / "annotations"))
    for position, sha in enumerate(shas):
        partial = list(iter_json(_build(repo, sha, tmp_path / f"at-{position}") / "annotations"))
        assert [row["history"] for row in partial] == [row["history"] for row in full[: position + 1]]
        assert [row["category"] for row in partial] == [row["category"] for row in full[: position + 1]]


def _build(repo, rev, out):
    build_dataset(repo=repo.path, rev=rev, out=out, workers=1, cache=None)
    return out


def categorized(subject, files, *, tags=(), body=""):
    records = {
        "message": {
            "subject": subject,
            "subject_tags": list(tags),
            "body": body,
            "revert": {"is_revert": subject.startswith("Revert")},
        },
        "features": {
            "is_doc_only": bool(files) and all(item[2] == "doc" for item in files),
            "is_test_only": bool(files) and all(item[2] == "test" for item in files),
        },
        "files": [
            {"status": status, "old_path": None if status == "A" else path, "new_path": path, "file_class": kind}
            for status, path, kind in files
        ],
    }
    return categorize(records)


@pytest.mark.parametrize(
    "subject, files, tags, primary, fix_like",
    [
        ('Revert "Fix x (#1)"', [("M", "src/a.py", "code")], (), "revert", False),
        ("[submodule] Update sonic-swss", [("M", "src/sonic-swss", "submodule")], ("submodule",), "submodule-bump",
         False),
        ("Fix typo in README", [("M", "README.md", "doc")], (), "docs", True),
        ("[Mellanox] Add SN5640 platform", [("A", "device/mellanox/x/port_config.ini", "config")], ("mellanox",),
         "platform-support", False),
        ("Upgrade FRR to 10.4.1", [("M", "rules/frr.mk", "build")], (), "dependency-bump", False),
        ("[swss] Fix crash on warm reboot", [("M", "src/a.py", "code")], ("swss",), "fix", True),
        ("[ci] tweak pipeline", [("M", ".azure-pipelines/a.yml", "config")], ("ci",), "build-ci", False),
        ("[bgp] Add knob for graceful restart", [("M", "src/a.py", "code")], ("bgp",), "feature", False),
        ("Tidy things", [("M", "src/a.py", "code")], (), "chore", False),
    ],
)
def test_categories(subject, files, tags, primary, fix_like):
    category = categorized(subject, files, tags=tags)
    assert category.change_type == primary
    assert category.fix_like is fix_like
    assert category.evidence and all(item.split(":")[0] in category.change_types for item in category.evidence)


def test_every_matching_type_is_listed_in_precedence_order():
    category = categorized("[Mellanox] Fix and add new SKU", [("A", "device/mellanox/x/sku/a.ini", "config")])
    assert category.change_types[:2] == ("platform-support", "fix") and category.fix_like


def test_body_issue_reference_marks_a_fix():
    assert categorized("Tidy things", [("M", "src/a.py", "code")], body="Fixes #12\n").fix_like
