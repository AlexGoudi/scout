import json
import re

import pyarrow.parquet as pq
import pytest

import run_scout
from conftest import ALICE, BOB, DAY, RepoBuilder
from scout_impl.dataset.build import build_dataset
from scout_impl.dataset.export import llm_document
from scout_impl.dataset.shards import iter_json
from scout_impl.dataset.split import assign_splits, split_labels
from unit_test_labels import fact

LABEL_KEYS = ("reverted", "bug_introducing", "fixed_by", "reverted_by", "revert_lead_days", "fix_lead_days")


def test_split_fractions_and_gaps():
    facts = [fact(index, landed=index * DAY) for index in range(1000)]
    split = assign_splits(facts)
    runs = []
    for name in split.assignment:
        if not runs or runs[-1][0] != name:
            runs.append([name, 0])
        runs[-1][1] += 1
    assert [tuple(run) for run in runs] == [
        ("train", 548), ("gap", 89), ("validation", 47), ("gap", 89), ("test", 137), ("gap", 90),
    ]
    assert split.ends == {"train": 637 * DAY, "validation": 773 * DAY, "test": 999 * DAY}


def test_exclusions_go_to_holdout():
    facts = [fact(index, landed=index * DAY) for index in range(300)]
    split = assign_splits(facts, [facts[5].sha, "deadbeef00"])
    assert split.assignment[5] == "holdout"
    assert (split.exclude_matched, split.exclude_unmatched) == (1, ("deadbeef00",))
    with pytest.raises(ValueError):
        assign_splits(facts, ["0000000"])


def test_split_labels_count_only_events_before_the_split_ends():
    ends = {"train": 100, "validation": 200, "test": 300}
    label = {
        "reverted": True, "reverted_landed": 150, "bug_introducing": True, "fixed_landed": 90,
        "reverted_within_7d": False, "reverted_within_30d": True, "reverted_within_90d": True,
    }
    assert split_labels(label, "train", ends) == {
        "bug_introducing": True, "reverted": False, "reverted_within_7d": False,
        "reverted_within_30d": False, "reverted_within_90d": False,
    }
    assert split_labels(label, "validation", ends)["reverted_within_90d"] is True
    merge = {key: None for key in label}
    assert set(split_labels(merge, "test", ends).values()) == {None}


def year_history(path):
    repo = RepoBuilder(path)
    shas = []
    repo.write("src/app/a.py", "".join(f"v{index} = {index}\n" for index in range(10)))
    repo.write("README.md", "Maintained by Alice Example <alice@example.com>\n")
    shas.append(repo.commit("Add app (#1)"))
    for step in range(1, 40):
        who = BOB if step % 3 else ALICE
        if step == 12:
            shas.append(repo.revert(shas[10], identity=who))
            continue
        if step % 5 == 0:
            content = (path / "src/app/a.py").read_text().replace(f"v{step % 10} =", f"v{step % 10} = 1 +")
            repo.write("src/app/a.py", content)
            shas.append(repo.commit(f"[app] Fix crash in v{step % 10} (#{step + 1})", identity=who, days=10))
        else:
            repo.write(f"dockers/docker-fpm-frr/t{step}.j2", f"t{step}\n")
            repo.write("src/app/a.py", (path / "src/app/a.py").read_text() + f"w{step} = {step}\n")
            shas.append(repo.commit(f"[bgp] add template {step} (#{step + 1})", identity=who, days=10))
    return repo, shas


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    root = tmp_path_factory.mktemp("dataset")
    repo, shas = year_history(root / "repo")
    exclude = root / "exclude.txt"
    exclude.write_text(f"# incidents\n{shas[20][:10]}\n")
    card = build_dataset(repo=repo.path, rev="HEAD", out=root / "a", workers=2, cache=root / "cache", exclude=exclude)
    build_dataset(repo=repo.path, rev="HEAD", out=root / "b", workers=1, cache=None, exclude=exclude)
    return repo, shas, root, card


def test_dataset_layout_and_card(dataset):
    repo, shas, root, card = dataset
    directory = root / "a"
    assert card["commits"]["total"] == len(shas)
    assert card["splits"]["exclude"] == {"matched": 1, "unmatched": []}
    assert set(card["splits"]["counts"]) <= {"train", "validation", "test", "gap", "holdout"}
    assert card["labels"]["reverts"]["linked_by_sha"] == 1
    assert card["labels"]["szz"]["fix_commits"] > 0
    assert json.loads((directory / "dataset_card.json").read_text()) == card
    assert "## Label positive rates" in (directory / "DATASET.md").read_text()
    annotations = list(iter_json(directory / "annotations"))
    assert [row["sha"] for row in annotations] == shas
    assert annotations[20]["split"] == "holdout"
    assert annotations[10]["labels"]["reverted_by"] == shas[12]


def test_feature_table(dataset):
    repo, shas, root, card = dataset
    table = pq.read_table(root / "a" / "features.parquet").to_pandas()
    assert list(table["sha"]) == shas
    assert table.columns.tolist() == card["columns"]
    assert {"component__packages", "area__bgp", "type__fix", "author_prior_commits", "churn"} <= set(table.columns)
    assert table.loc[20, "split"] == "holdout"
    assert table["area__bgp"].sum() == sum(1 for index in range(1, 40) if index % 5 and index != 12)
    label_columns = [column for column in table.columns if column.startswith("label_")]
    assert "label_bug_introducing" in label_columns and "label_reverted_within_90d" in label_columns


def test_llm_documents_are_label_free_and_within_budget(dataset):
    repo, shas, root, card = dataset
    documents = list(iter_json(root / "a" / "llm"))
    assert [document["sha"] for document in documents] == shas
    for document in documents:
        assert set(document) == {"sha", "committed_at", "doc_version", "chars", "diff_complete", "text"}
        assert document["chars"] == len(document["text"]) <= card["filters"]["llm_char_budget"]
        assert not any(key in document["text"] for key in LABEL_KEYS if "_" in key)


def test_llm_document_budget_truncates_the_diff():
    record = {
        "commit": {"sha": "a" * 40, "committed_at": "2020-01-01T00:00:00Z"},
        "message": {
            "subject": "Big change", "subject_tags": [], "pr_number": None, "body": "",
            "sections": {"why": "Because " * 50, "how": None, "verify": None},
            "revert": {"is_revert": False, "reverts_sha": None, "reverts_pr": None},
        },
        "submodules": [],
        "areas": {"components": [], "features": [], "entities": []},
        "files": [
            {"status": "M", "old_path": f"f{index}", "new_path": f"f{index}", "file_class": "code", "binary": False,
             "additions": 100, "deletions": 0, "patch": "@@ -0,0 +1,100 @@\n" + "+line\n" * 100,
             "patch_truncated": False, "scopes": []}
            for index in range(10)
        ],
    }
    document = llm_document(record, budget=2000)
    assert document["chars"] <= 2000 and not document["diff_complete"]
    assert document["text"].endswith("[truncated]") and "--- f0" in document["text"]
    assert llm_document(record)["diff_complete"]


def test_rebuilds_are_byte_identical(dataset):
    repo, shas, root, card = dataset
    first, second = root / "a", root / "b"
    names = sorted(path.relative_to(first) for path in first.rglob("*") if path.is_file())
    assert names == sorted(path.relative_to(second) for path in second.rglob("*") if path.is_file())
    for name in names:
        assert (first / name).read_bytes() == (second / name).read_bytes(), name


def test_no_email_or_author_name_in_any_output(dataset):
    repo, shas, root, card = dataset
    email = re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
    for path in (root / "a").rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            if path.suffix == ".gz":
                import gzip

                data = gzip.decompress(data)
            assert not email.search(data), path
            for name in (b"Alice", b"Bob Builder"):
                assert name not in data, (path, name)


def test_cli_builds_a_dataset(dataset, tmp_path, capsys):
    repo, shas, root, card = dataset
    arguments = ["dataset", "build", "--repo", str(repo.path), "--rev", "HEAD", "--out", str(tmp_path / "cli"),
                 "--workers", "1", "--cache", "", "--no-szz"]
    assert run_scout.main(arguments) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["commits"]["total"] == len(shas)
    assert summary["labels"]["bug_introducing"]["known"] == 0
