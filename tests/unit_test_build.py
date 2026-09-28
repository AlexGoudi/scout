import pytest

from conftest import build_history
from scout_impl.dataset.build import RecordCache, mine_records
from scout_impl.dataset.shards import iter_lines, write_shards
from scout_impl.mining.gitio import Git
from scout_impl.mining.record import make_context, mine_commit, record_to_json


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    repo, _ = build_history(tmp_path_factory.mktemp("build") / "repo")
    git = Git(repo.path)
    shas = git.first_parent_shas("HEAD")
    context = make_context(git, "HEAD")
    expected = [record_to_json(mine_commit(git, sha, context)) for sha in shas]
    return git, shas, context, expected


def mine(sample, cache_root, workers):
    git, shas, context, _ = sample
    cache = RecordCache(cache_root, context)
    return list(mine_records(git, shas, context, names_revision="HEAD", cache=cache, workers=workers, chunk_size=3))


def test_pooled_chunks_match_single_commit_mining(sample):
    assert mine(sample, None, workers=3) == sample[3]


def test_cache_round_trip_is_identical(sample, tmp_path):
    cold = mine(sample, tmp_path / "cache", workers=2)
    cached = RecordCache(tmp_path / "cache", sample[2])
    assert all(cached.has(sha) for sha in sample[1])
    assert mine(sample, tmp_path / "cache", workers=2) == cold == sample[3]


def test_cache_key_changes_with_the_salt(sample, tmp_path):
    git, _, context, _ = sample
    salted = make_context(git, "HEAD", salt="other")
    assert RecordCache(tmp_path, context).key != RecordCache(tmp_path, salted).key


def test_corrupt_cache_entries_are_re_mined(sample, tmp_path):
    mine(sample, tmp_path / "cache", workers=1)
    cache = RecordCache(tmp_path / "cache", sample[2])
    cache._path(sample[1][0]).write_bytes(b"not gzip")
    assert mine(sample, tmp_path / "cache", workers=1) == sample[3]


def test_shards_are_ordered_and_byte_identical(sample, tmp_path):
    names = write_shards(tmp_path / "a", sample[3], size=4)
    write_shards(tmp_path / "b", sample[3], size=4)
    assert names == [f"part-{index:05d}.jsonl.gz" for index in range(len(names))]
    assert list(iter_lines(tmp_path / "a")) == sample[3]
    for name in names:
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()
