"""Incremental calibration cache behaviour."""

from pathlib import Path

from scout_impl.eval._cache import Step, fingerprint, load_manifest, seed_legacy_sha_subdirs
from scout_impl.eval.incidents_cache import run_incidents_cache

from conftest import TempGitRepo


def test_step_fresh_skips_until_fingerprint_changes(tmp_path: Path) -> None:
    step = Step(tmp_path, "demo", fingerprint("a"), refresh=False)
    assert not step.fresh()
    step.commit(["one.json"], {"tip": "abc"})
    step2 = Step(tmp_path, "demo", fingerprint("a"), refresh=False)
    assert step2.fresh()
    step3 = Step(tmp_path, "demo", fingerprint("b"), refresh=False)
    assert not step3.fresh()


def test_seed_legacy_sha_subdirs_copies_newest(tmp_path: Path) -> None:
    stable = tmp_path / "sonic-mgmt"
    legacy = stable / ("deadbeef" * 5)
    legacy.mkdir(parents=True)
    (legacy / "git-overview.json").write_text('{"tip_sha":"x"}\n', encoding="utf-8")
    (legacy / "model-index.json").write_text("{}\n", encoding="utf-8")
    seed_legacy_sha_subdirs(stable)
    assert (stable / "git-overview.json").is_file()
    assert (stable / "model-index.json").is_file()


def test_incidents_incremental_appends(temp_repo: TempGitRepo, tmp_path: Path) -> None:
    temp_repo.write("a.txt", "1\n")
    first = temp_repo.commit("init", date="2026-01-01T00:00:00+00:00")
    cache = tmp_path / "cal"
    repo = temp_repo.as_repo()
    run_incidents_cache(repo, cache, revision=first, refresh=True)
    manifest = load_manifest(cache)
    assert manifest["steps"]["incidents"]["cursor"]["tip"] == first

    temp_repo.write("b.txt", "2\n")
    temp_repo.commit(
        'Revert "init"\n\nThis reverts commit %s.\n' % first,
        date="2026-02-01T00:00:00+00:00",
    )
    tip = repo.rev_parse("HEAD")
    run_incidents_cache(repo, cache, revision=tip, refresh=False)
    incidents = run_incidents_cache(repo, cache, revision=tip, refresh=False)
    assert len(incidents) == 1
