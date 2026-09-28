import pytest

from scout_impl.mining.diff import PatchLimits, iter_commit_patches, merge_patches
from scout_impl.mining.gitio import PATCH_MARKER
from scout_impl.mining.lines import HASH, JINJA, SLASH, comment_markers, indentation_matters, line_kind, squeeze


@pytest.mark.parametrize(
    "path, markers",
    [
        ("src/app.py", HASH),
        ("files/scripts/arp_update", HASH),
        ("platform/Makefile.work", HASH),
        ("src/sai/sai.c", SLASH),
        ("models/sonic-port.yang", SLASH),
        ("dockers/docker-fpm-frr/Dockerfile.j2", JINJA + HASH),
        ("files/build_templates/config_db.json.j2", JINJA),
        ("device/x/port_config.json", ()),
        ("src/frr/patch/0001-fix.patch", ()),
    ],
)
def test_comment_markers_follow_the_file_name(path, markers):
    assert comment_markers(path) == markers


@pytest.mark.parametrize(
    "content, path, kind",
    [
        (b"   \t", "a.py", "blank"),
        (b"    # explain", "a.py", "comment"),
        (b'x = "#"', "a.py", "code"),
        (b"#!/usr/bin/env python3", "a.py", "code"),
        (b"#!/bin/bash", "files/scripts/arp_update", "code"),
        (b"#include <stdio.h>", "a.c", "code"),
        (b"#define LIMIT 4", "a.c", "code"),
        (b" * a doc comment line", "a.c", "comment"),
        (b" *", "a.c", "comment"),
        (b"*ptr = 0;", "a.c", "code"),
        (b"  // note", "a.yang", "comment"),
        (b"{# jinja #}", "t.conf.j2", "comment"),
        (b"# conf comment", "t.conf.j2", "comment"),
        (b'{"a": 1}', "c.json", "code"),
    ],
)
def test_line_kind(content, path, kind):
    assert line_kind(content, comment_markers(path)) == kind


@pytest.mark.parametrize(
    "path, matters",
    [
        ("src/app.py", True),
        (".azure-pipelines/build.yml", True),
        ("files/build_templates/docker-compose.yaml.j2", True),
        ("rules/functions.mk", True),
        ("platform/Makefile.work", True),
        ("src/sonic-utilities/debian/rules", True),
        ("src/sai/sai.c", False),
        ("models/sonic-port.yang", False),
        ("files/scripts/arp_update", False),
        (None, False),
    ],
)
def test_indentation_matters_in_python_yaml_and_makefiles(path, matters):
    assert indentation_matters(path) is matters


@pytest.mark.parametrize(
    "old, new, same",
    [
        (b"  x = f(a,\t b)  ", b"x = f(a, b)", True),
        (b'$(info "CERT" : "$(CERT)")', b'$(info "CERT"     : "$(CERT)")', True),
        (b"# don't do this\r", b"# don't do this", True),
        (b"x = f(a,b)", b"x = f(a, b)", False),
        (b"[ \"$mode\" = \"true\" ] && rv = 0", b"[ \"$mode\" = \"true\" ] && rv=0", False),
        (b'MNT= " -v /run:/run"', b'MNT=" -v /run:/run"', False),
        (b"cmd = 'cat' + path", b"cmd = 'cat ' + path", False),
        (b'name = "PSU{}".format(i)', b'name = "PSU {}".format(i)', False),
        (b's = "a\\" b"', b's = "a\\"  b"', False),
    ],
)
def test_whitespace_inside_quotes_is_part_of_the_line(old, new, same):
    assert (squeeze(old) == squeeze(new)) is same


def patch_stream(*files, sha="a" * 40):
    lines = [PATCH_MARKER + sha.encode(), b""]
    for path, body in files:
        lines += [f"diff --git a/{path} b/{path}".encode(), b"index 1111111..2222222 100644",
                  f"--- a/{path}".encode(), f"+++ b/{path}".encode(), *body]
    return lines


def parse(*files, limits=PatchLimits()):
    [(sha, patches)] = list(iter_commit_patches(patch_stream(*files), limits))
    return {key[1]: merge_patches(found) for key, found in patches.items()}


def test_scopes_come_from_hunk_headers_in_order_without_repeats():
    body = [b"@@ -10 +10 @@ def load(self):", b"-a = 1", b"+a = 2",
            b"@@ -20 +20 @@ def load(self):", b"-b = 1", b"+b = 2",
            b"@@ -1 +1 @@", b"-c", b"+d",
            b"@@ -30 +30 @@ class Loader:", b"-e", b"+f"]
    patch = parse(("src/app.py", body))["src/app.py"]
    assert patch.scopes == ["def load(self):", "class Loader:"]


def test_line_kinds_pair_reindented_lines_and_count_comments_and_blanks():
    body = [b"@@ -1,5 +1,5 @@ run() {",
            b"-    if ready; then", b"-# old note", b"-", b"-x=1", b"-    call",
            b"+        if ready; then", b"+# new note", b"+", b"+x=2", b"+      call "]
    kinds = parse(("src/app.sh", body))["src/app.sh"].line_kinds
    assert kinds == {"blank": [1, 1], "whitespace_only": [2, 2], "comment": [1, 1]}


def test_reindenting_python_is_code_but_other_whitespace_is_not():
    body = [b"@@ -1,3 +1,3 @@ def run():",
            b"-    if ready:", b"-    call()  ", b"-    x = f(a,  b)\r",
            b"+        if ready:", b"+    call()", b"+    x = f(a, b)"]
    kinds = parse(("src/app.py", body))["src/app.py"].line_kinds
    assert kinds == {"blank": [0, 0], "whitespace_only": [2, 2], "comment": [0, 0]}


def test_a_reindented_comment_counts_once_as_whitespace_only():
    body = [b"@@ -1 +1 @@", b"-# note", b"+    # note"]
    assert parse(("a.sh", body))["a.sh"].line_kinds == {"blank": [0, 0], "whitespace_only": [1, 1],
                                                        "comment": [0, 0]}


def test_pairing_stays_inside_one_hunk():
    body = [b"@@ -1 +0,0 @@", b"-value", b"@@ -0,0 +5 @@", b"+  value"]
    assert parse(("a.sh", body))["a.sh"].line_kinds["whitespace_only"] == [0, 0]


def test_counts_cover_lines_past_the_patch_cap():
    body = [b"@@ -0,0 +1,1000 @@"] + [b"+# comment %d" % index for index in range(1000)]
    patch = parse(("big.py", body), limits=PatchLimits(max_lines_per_file=400))["big.py"]
    assert len(patch.lines) == 400 and patch.truncated
    assert patch.line_kinds["comment"] == [1000, 0]


def test_c_preprocessor_lines_are_code():
    body = [b"@@ -0,0 +1,3 @@", b"+#include <stdio.h>", b"+/* license */", b"+int x;"]
    assert parse(("a.c", body))["a.c"].line_kinds["comment"] == [1, 0]
