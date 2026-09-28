import json
import subprocess

import pytest
from jsonschema import Draft7Validator

import run_scout
from conftest import BOB, SCOUT_ROOT, SWSS_NEW, SWSS_OLD, build_history
from scout_impl.mining.gitio import Git, GitError
from scout_impl.mining.record import extract_records, make_context, mine_commit, record_to_dict, record_to_json

SCHEMA = json.loads((SCOUT_ROOT / "schemas" / "scout-commit-1.1.json").read_text())


@pytest.fixture(scope="module")
def history(tmp_path_factory):
    repo, shas = build_history(tmp_path_factory.mktemp("history") / "repo")
    git = Git(repo.path)
    context = make_context(git, "HEAD")
    records = {name: record_to_dict(mine_commit(git, sha, context)) for name, sha in shas.items()}
    return repo, shas, git, context, records


def files_by_path(record):
    return {item["new_path"] or item["old_path"]: item for item in record["files"]}


def test_every_record_matches_the_schema(history):
    validator = Draft7Validator(SCHEMA)
    for name, record in history[4].items():
        errors = sorted(validator.iter_errors(record), key=lambda error: list(error.path))
        assert not errors, f"{name}: {errors[0].message} at {list(errors[0].path)}"


def test_root_commit_diffs_against_the_empty_tree(history):
    record = history[4]["root"]
    assert record["commit"]["is_root"] and record["commit"]["base_strategy"] == "empty-tree"
    main = files_by_path(record)["src/app/main.py"]
    assert (main["status"], main["additions"], main["hunks"]) == ("A", 20, [[0, 0, 1, 20]])


def test_ordinary_commit(history):
    record = history[4]["ordinary"]
    main = files_by_path(record)["src/app/main.py"]
    assert (main["status"], main["hunks"], main["patch"]) == ("M", [[0, 0, 1, 1]], "@@ -0,0 +1 @@\n+line 0\n")
    assert [entity["id"] for entity in record["areas"]["entities"]] == [
        "vendor:acme",
        "platform:x86_64-acme-r0",
        "hwsku:ACME-1",
    ]
    assert record["message"]["pr_number"] == 2 and record["message"]["subject_tags"] == ["acme"]
    assert record["features"]["file_count"] == 2 and record["features"]["churn"] == 2
    assert record["commit"]["author_id"] != history[4]["root"]["commit"]["author_id"]


def test_rename(history):
    renamed = history[4]["rename"]["files"]
    assert len(renamed) == 1
    item = renamed[0]
    assert (item["status"], item["old_path"], item["new_path"]) == ("R", "src/app/main.py", "src/app/core.py")
    assert item["similarity"] >= 50 and (item["additions"], item["deletions"]) == (1, 0)


def test_binary_file(history):
    (item,) = history[4]["binary"]["files"]
    assert item["binary"] and item["file_class"] == "binary"
    assert item["additions"] is None and item["patch"] is None
    assert history[4]["binary"]["warnings"] == []


def test_mode_change(history):
    (item,) = history[4]["mode"]["files"]
    assert (item["status"], item["old_mode"], item["new_mode"], item["hunks"]) == ("M", "100644", "100755", [])


def test_delete(history):
    (item,) = history[4]["delete"]["files"]
    assert (item["status"], item["new_path"], item["new_blob"], item["hunks"]) == ("D", None, None, [[1, 1, 0, 0]])


def test_submodule_add_and_bump(history):
    added = history[4]["submodule_add"]
    assert added["submodules"] == [
        {
            "name": "src/sonic-swss",
            "new_sha": SWSS_OLD,
            "old_sha": None,
            "path": "src/sonic-swss",
            "url": "https://github.com/sonic-net/sonic-swss",
        }
    ]
    bump = history[4]["submodule_bump"]
    (item,) = bump["files"]
    assert item["file_class"] == "submodule" and (item["old_blob"], item["new_blob"]) == (SWSS_OLD, SWSS_NEW)
    assert bump["submodules"][0]["url"] == "https://github.com/sonic-net/sonic-swss"
    assert bump["commit"]["author_is_bot"]
    assert bump["features"]["is_submodule_only"] and bump["message"]["pulled_commit_lines"] == 1
    assert "Stranger" not in json.dumps(bump)
    assert [entity["id"] for entity in bump["areas"]["entities"]] == ["submodule:src/sonic-swss"]


def test_merge_diffs_against_the_first_parent(history):
    _, shas, *_ = history
    record = history[4]["merge"]
    assert record["commit"]["is_merge"] and record["commit"]["parents"][0] == shas["mainline"]
    assert [item["new_path"] for item in record["files"]] == ["src/app/core.py"]


def test_revert_links_to_the_full_sha(history):
    _, shas, *_ = history
    revert = history[4]["revert"]["message"]["revert"]
    assert revert == {
        "depth": 1,
        "is_nested": False,
        "is_revert": True,
        "reverts_pr": 2,
        "reverts_sha": shas["ordinary"],
    }


def test_patch_caps_keep_every_hunk_range(history):
    (item,) = history[4]["big"]["files"]
    assert item["patch_truncated"] and item["hunks"] == [[0, 0, 1, 1001]]
    lines = item["patch"].splitlines()
    assert len(lines) <= 401
    assert all(len(line.encode()) <= 2000 for line in lines)


def test_quoted_paths_round_trip(history):
    paths = sorted(files_by_path(history[4]["paths"]))
    assert paths == ['docs/q"uote.md', "docs/tab\tname.md", "docs/with space.md", "docs/\u00fcn\u00ef.md"]


def test_no_names_or_emails_leave_the_extractor(history):
    text = "\n".join(record_to_json(record) for record in history[4].values())
    for needle in ("Alice", "alice@", "Bob Builder", "bob@", "example.com", "sonicbld@"):
        assert needle not in text


def test_submodule_urls_are_redacted(repo):
    handle = ("lguohan", "lguohan@example.com")
    repo.write("README.md", "x\n")
    repo.commit("Initial import", identity=handle)
    repo.gitlink("src/p4-switch", SWSS_OLD, url="https://github.com/lguohan/switch")
    sha = repo.commit("Add p4 switch", identity=BOB, stage_all=False)
    git = Git(repo.path)
    record = record_to_dict(mine_commit(git, sha, make_context(git, "HEAD")))
    assert record["submodules"][0]["url"] == "https://github.com/<name>/switch"


YANG = "module sonic-x {{\n    container X {{\n        leaf speed {{\n            type {};\n        }}\n    }}\n}}\n"
PYTHON = "class Loader:\n    def load(self):\n{}        a = {}\n        return a\n"


def test_scopes_and_line_kinds(repo):
    repo.write("src/app.py", PYTHON.format("", 1))
    repo.write("models/sonic-x.yang", YANG.format("string"))
    repo.commit("Initial import")
    repo.write("src/app.py", PYTHON.format("", 2))
    repo.write("models/sonic-x.yang", YANG.format("uint32"))
    logic = repo.commit("Change types")
    repo.write("src/app.py", PYTHON.format("        # why a is 2\n\n", 2))
    comment = repo.commit("Explain a")
    repo.write("src/app.py", PYTHON.format("        # why a is 2\n\n", 2).replace("        a = 2", "\ta = 2"))
    repo.write("models/sonic-x.yang", YANG.format("uint32").replace("        leaf speed", "\tleaf speed"))
    reindent = repo.commit("Use tabs")
    git = Git(repo.path)
    context = make_context(git, "HEAD")
    records = {name: record_to_dict(mine_commit(git, sha, context))
               for name, sha in (("logic", logic), ("comment", comment), ("reindent", reindent))}

    scopes = {item["new_path"]: item["scopes"] for item in records["logic"]["files"]}
    assert scopes == {"src/app.py": ["def load(self):"], "models/sonic-x.yang": ["leaf speed {"]}
    features = records["logic"]["features"]
    assert (features["logic_churn"], features["scope_count"], features["is_comment_or_whitespace_only"]) == (
        4, 2, False)

    features = records["comment"]["features"]
    assert (features["comment_line_count"], features["blank_line_count"], features["logic_churn"]) == (1, 1, 0)
    assert features["is_comment_or_whitespace_only"]
    assert records["comment"]["files"][0]["line_kinds"] == {"blank": [1, 0], "whitespace_only": [0, 0],
                                                            "comment": [1, 0]}

    features = records["reindent"]["features"]
    assert (features["whitespace_only_line_count"], features["logic_churn"]) == (2, 2)
    assert not features["is_comment_or_whitespace_only"]
    by_path = {item["new_path"]: item["line_kinds"]["whitespace_only"] for item in records["reindent"]["files"]}
    assert by_path == {"src/app.py": [0, 0], "models/sonic-x.yang": [1, 1]}


def test_streaming_matches_single_commit_mining(history):
    repo, shas, git, context, records = history
    ordered = git.first_parent_shas("HEAD")
    streamed = {record.commit.sha: record_to_dict(record) for record in extract_records(git, ordered, context)}
    assert list(streamed) == ordered
    for name, sha in shas.items():
        if sha in streamed:
            assert streamed[sha] == records[name], name


def test_identical_histories_give_byte_identical_records(history, tmp_path):
    repo, shas, git, context, records = history
    twin, twin_shas = build_history(tmp_path / "twin")
    assert twin_shas == shas
    twin_git = Git(twin.path)
    twin_context = make_context(twin_git, "HEAD")
    for sha in twin_git.first_parent_shas("HEAD"):
        assert record_to_json(mine_commit(twin_git, sha, twin_context)) == record_to_json(
            mine_commit(git, sha, context)
        )


def test_mining_leaves_the_clone_untouched(history):
    repo, shas, git, context, _ = history
    assert repo.git("status", "--porcelain") == ""

    def state():
        control = repo.path / ".git"
        return sorted((path.relative_to(control), path.stat().st_mtime_ns) for path in control.rglob("*"))

    before = state()
    list(extract_records(git, git.first_parent_shas("HEAD"), context))
    assert state() == before


def test_cli_writes_the_pretty_record(history, tmp_path):
    repo, shas, git, context, records = history
    output = tmp_path / "record.json"
    arguments = ["mine", "commit", "--repo", str(repo.path), "--commit", shas["merge"], "--output", str(output)]
    assert run_scout.main(arguments) == 0
    assert json.loads(output.read_text()) == records["merge"]
    assert output.read_text() == record_to_json(mine_commit(git, shas["merge"], context), pretty=True)


def test_shallow_clone_is_rejected(history, tmp_path):
    repo = history[0]
    clone = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{repo.path}", str(clone)], check=True, capture_output=True
    )
    git = Git(clone)
    with pytest.raises(GitError, match="shallow"):
        list(extract_records(git, [git.resolve_commit("HEAD")], make_context(git, "HEAD")))
