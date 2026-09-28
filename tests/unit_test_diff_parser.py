import functools

from scout_impl.diffparse import parse_diff as _parse_diff
from scout_impl.models import (
    CHANGE_ADDED,
    CHANGE_DELETED,
    CHANGE_MODIFIED,
    CHANGE_RENAMED,
)
from scout_impl.repos import resolve_adapter
from scout_impl.repos.sonic_mgmt import ANSIBLE_CODE, ANSIBLE_DATA

# The parser is repo-agnostic and takes the adapter whose rules classify the paths.
parse_diff = functools.partial(_parse_diff, adapter=resolve_adapter("sonic-mgmt"))

GOLDEN_CONFIG_PATH = "ansible/library/generate_golden_config_db.py"

MODIFIED_DIFF = (
    f"diff --git a/{GOLDEN_CONFIG_PATH} b/{GOLDEN_CONFIG_PATH}\n"
    "index 1111111..2222222 100644\n"
    f"--- a/{GOLDEN_CONFIG_PATH}\n"
    f"+++ b/{GOLDEN_CONFIG_PATH}\n"
    "@@ -155,6 +155,7 @@ def generate_port_entries(self):\n"
    "             port_entry = {\n"
    "                 'alias': alias,\n"
    "                 'lanes': sub_lanes,\n"
    "-                'speed': str(speed_mbps),\n"
    "+                'speed': str(speed_mbps),\n"
    "+                'admin_status': 'up',\n"
    "                 'mtu': '9100',\n"
    "             }\n"
)


def test_modified_file_records_line_numbers_on_both_sides() -> None:
    file_diffs = parse_diff(MODIFIED_DIFF)

    assert len(file_diffs) == 1
    file_diff = file_diffs[0]
    assert file_diff.path == GOLDEN_CONFIG_PATH
    assert file_diff.change_type == CHANGE_MODIFIED
    assert file_diff.path_class == ANSIBLE_CODE
    assert file_diff.old_path is None
    assert file_diff.additions == 2
    assert file_diff.deletions == 1

    hunk = file_diff.hunks[0]
    assert (hunk.old_start, hunk.old_count, hunk.new_start, hunk.new_count) == (155, 6, 155, 7)
    assert hunk.section == "def generate_port_entries(self):"

    added = file_diff.added_lines
    assert [line.new_lineno for line in added] == [158, 159]
    assert added[1].content == "                'admin_status': 'up',"
    assert added[1].old_lineno is None

    removed = file_diff.removed_lines
    assert [line.old_lineno for line in removed] == [158]
    assert removed[0].new_lineno is None


def test_context_lines_carry_both_line_numbers() -> None:
    hunk = parse_diff(MODIFIED_DIFF)[0].hunks[0]

    first_context = hunk.lines[0]
    assert first_context.kind == " "
    assert (first_context.old_lineno, first_context.new_lineno) == (155, 155)

    last_context = hunk.lines[-1]
    assert (last_context.old_lineno, last_context.new_lineno) == (160, 161)


def test_added_and_deleted_files() -> None:
    diff = (
        "diff --git a/tests/common/helpers/new.py b/tests/common/helpers/new.py\n"
        "new file mode 100644\n"
        "index 0000000..3333333\n"
        "--- /dev/null\n"
        "+++ b/tests/common/helpers/new.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+import os\n"
        "+print(os.getcwd())\n"
        "diff --git a/tests/common/helpers/old.py b/tests/common/helpers/old.py\n"
        "deleted file mode 100644\n"
        "index 4444444..0000000\n"
        "--- a/tests/common/helpers/old.py\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-gone = True\n"
    )

    added, deleted = parse_diff(diff)

    assert (added.path, added.change_type) == ("tests/common/helpers/new.py", CHANGE_ADDED)
    assert [line.new_lineno for line in added.added_lines] == [1, 2]
    assert (deleted.path, deleted.change_type) == ("tests/common/helpers/old.py", CHANGE_DELETED)
    assert [line.old_lineno for line in deleted.removed_lines] == [1]
    assert deleted.old_path is None


def test_rename_keeps_both_paths_and_similarity() -> None:
    diff = (
        "diff --git a/ansible/vars/topo_old.yml b/ansible/vars/topo_new.yml\n"
        "similarity index 95%\n"
        "rename from ansible/vars/topo_old.yml\n"
        "rename to ansible/vars/topo_new.yml\n"
        "index 5555555..6666666 100644\n"
        "--- a/ansible/vars/topo_old.yml\n"
        "+++ b/ansible/vars/topo_new.yml\n"
        "@@ -1,2 +1,2 @@\n"
        " topology:\n"
        "-  host_interfaces: [0, 1]\n"
        "+  host_interfaces: [0, 1, 2]\n"
    )

    file_diff = parse_diff(diff)[0]

    assert file_diff.change_type == CHANGE_RENAMED
    assert file_diff.path == "ansible/vars/topo_new.yml"
    assert file_diff.old_path == "ansible/vars/topo_old.yml"
    assert file_diff.similarity == 95
    assert file_diff.path_class == ANSIBLE_DATA


def test_binary_file_is_flagged_without_hunks() -> None:
    diff = (
        "diff --git a/ansible/files/image.bin b/ansible/files/image.bin\n"
        "index 7777777..8888888 100644\n"
        "Binary files a/ansible/files/image.bin and b/ansible/files/image.bin differ\n"
    )

    file_diff = parse_diff(diff)[0]

    assert file_diff.is_binary is True
    assert file_diff.hunks == []


def test_hunk_header_without_counts_defaults_to_one_line() -> None:
    diff = (
        "diff --git a/README.md b/README.md\n"
        "index 9999999..aaaaaaa 100644\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -7 +7 @@\n"
        "-old\n"
        "+new\n"
    )

    hunk = parse_diff(diff)[0].hunks[0]

    assert (hunk.old_start, hunk.old_count, hunk.new_start, hunk.new_count) == (7, 1, 7, 1)


def test_no_newline_marker_is_not_a_diff_line() -> None:
    diff = (
        "diff --git a/tests/common/x.py b/tests/common/x.py\n"
        "index bbbbbbb..ccccccc 100644\n"
        "--- a/tests/common/x.py\n"
        "+++ b/tests/common/x.py\n"
        "@@ -1 +1 @@\n"
        "-a = 1\n"
        "\\ No newline at end of file\n"
        "+a = 2\n"
        "\\ No newline at end of file\n"
    )

    hunk = parse_diff(diff)[0].hunks[0]

    assert [line.kind for line in hunk.lines] == ["-", "+"]


def test_empty_diff_yields_no_files() -> None:
    assert parse_diff("") == []
