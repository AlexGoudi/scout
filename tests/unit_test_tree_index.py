"""The tree index and its content-addressed blob cache."""

import pytest

from scout_impl.static.fixtures import FixtureError, TreeFixture
from scout_impl.static.treeindex import TreeIndex


def _index(files, **kwargs):
    fixture = TreeFixture.from_files(files, **kwargs)
    return TreeIndex(fixture.source(), fixture.rev)


def test_a_listing_answers_presence_without_reading_anything():
    tree = _index({"a/b.txt": "one", "c.txt": "two"})
    assert tree.exists("a/b.txt")
    assert not tree.exists("a/missing.txt")
    assert tree.paths == ["a/b.txt", "c.txt"]
    assert tree.blob_reads == 0


def test_identical_content_is_one_blob_and_is_read_once():
    """The 156 platforms declaring `broadcom` share a blob, so they share a round trip."""
    tree = _index({f"device/v/p{n}/platform_asic": "broadcom\n" for n in range(10)})
    contents = [tree.read(path) for path in tree.paths]

    assert contents == ["broadcom\n"] * 10
    assert tree.blob_reads == 1
    assert tree.cache_hits == 9


def test_different_content_is_charged_separately():
    tree = _index({"a": "one", "b": "two", "c": "one"})
    for path in ("a", "b", "c"):
        tree.read(path)
    assert tree.blob_reads == 2
    assert tree.cache_hits == 1


def test_a_repeated_read_of_one_path_is_charged_once():
    tree = _index({"a": "one"})
    tree.read("a")
    tree.read("a")
    assert tree.blob_reads == 1
    assert tree.cache_hits == 1


def test_reading_a_path_the_listing_does_not_hold_raises():
    tree = _index({"a": "one"})
    with pytest.raises(KeyError):
        tree.read("b")


def test_the_mode_of_an_entry_is_available_without_reading_it():
    tree = _index({"link": ("120000", "target"), "file": ("100644", "body")})
    assert tree.is_symlink("link")
    assert not tree.is_symlink("file")
    assert tree.blob_reads == 0


def test_total_paths_reports_the_whole_tree_not_the_filtered_listing():
    """A pinned fixture lists a subset on purpose and still knows how big the tree was."""
    tree = _index({"a": "one", "b": "two"}, tree_paths=20155)
    assert len(tree.paths) == 2
    assert tree.total_paths == 20155


def test_prefetch_asks_for_distinct_blobs_not_distinct_paths():
    """The dedupe that makes rule C6 affordable: 1,853 links, 493 targets, 493 asked for."""
    asked = []

    fixture = TreeFixture.from_files({f"link{n}": ("120000", "target-a" if n % 2 else "target-b")
                                      for n in range(20)})
    source = fixture.source()
    source.prefetch_blobs = lambda shas: asked.extend(shas) or len(shas)
    tree = TreeIndex(source, fixture.rev)

    assert tree.prefetch(tree.paths) == 2
    assert len(asked) == 2, "twenty paths, two distinct targets between them"


def test_prefetch_skips_blobs_already_in_the_read_memo():
    asked = []
    fixture = TreeFixture.from_files({"a": "one", "b": "two"})
    source = fixture.source()
    source.prefetch_blobs = lambda shas: asked.append(list(shas)) or len(shas)
    tree = TreeIndex(source, fixture.rev)

    tree.read("a")
    tree.prefetch(["a", "b"])
    assert len(asked) == 1 and len(asked[0]) == 1, "only b was still missing"


def test_prefetch_is_advisory_and_a_source_that_cannot_do_it_still_reads():
    """A working copy and a fixture hold everything already; the no-op is the right answer."""
    tree = _index({"a": "one"})
    assert tree.prefetch(["a"]) == 0
    assert tree.read("a") == "one"


def test_prefetching_a_path_the_listing_does_not_hold_asks_for_nothing():
    asked = []
    fixture = TreeFixture.from_files({"a": "one"})
    source = fixture.source()
    source.prefetch_blobs = lambda shas: asked.extend(shas) or len(shas)
    tree = TreeIndex(source, fixture.rev)

    assert tree.prefetch(["nowhere"]) == 0
    assert asked == []
    assert tree.prefetches == 0, "an empty batch is not a batch"


def test_a_fixture_source_refuses_to_shell_out_to_git():
    """NFR-10 in code: an analyzer reaching behind the source API fails loudly."""
    fixture = TreeFixture.from_files({"a": "one"})
    with pytest.raises(FixtureError) as raised:
        fixture.source().git("log")
    assert "offline by construction" in str(raised.value)


def test_a_fixture_serving_a_path_whose_blob_it_lacks_raises_rather_than_returning_empty():
    fixture = TreeFixture.from_files({"a": "one"})
    stripped = fixture.source()
    stripped.fixture.blobs.clear()
    with pytest.raises(FixtureError) as raised:
        stripped.read_file(fixture.rev, "a")
    assert "did not capture its blob" in str(raised.value)
