"""PR file numstat fetch modes (merge-base, merge ref, deepen fallbacks)."""

from __future__ import annotations

from ..gitcmd import GitError, GitRepo
from ..mining.gitio import parse_numstat_text


def parse_numstat(raw: str) -> list:
    return parse_numstat_text(raw)


def git_numstat(repo: GitRepo, left: str, right: str) -> list:
    raw = repo.run("diff", "--numstat", left, right)
    return parse_numstat(raw)


def ref_exists(repo: GitRepo, ref: str) -> bool:
    resolved = repo.run("rev-parse", "--verify", "--quiet", ref, check=False).strip()
    return bool(resolved)


def path_bag_usable(source: str | None) -> bool:
    if not source:
        return False
    if "two_dot" in source or source in {"unresolved", "fetch_failed", "diff_failed"}:
        return False
    return True


def files_for_model(files, source: str | None):
    if not path_bag_usable(source):
        return []
    return files or []


def fetch_pr_files(repo: GitRepo, ref: str, pr: int):
    tmp = f"refs/tmp/pr-{pr}"
    if not ref_exists(repo, tmp):
        try:
            repo.run("fetch", "-q", "origin", f"pull/{pr}/head:{tmp}")
        except GitError:
            return None, "fetch_failed"
    try:
        merge_base = repo.run("merge-base", ref, tmp).strip()
        return git_numstat(repo, merge_base, tmp), "fetch_pull_head_merge_base"
    except GitError:
        pass
    merge_tmp = f"refs/tmp/pr-{pr}-merge"
    try:
        if not ref_exists(repo, merge_tmp):
            repo.run("fetch", "-q", "origin", f"pull/{pr}/merge:{merge_tmp}")
        return git_numstat(repo, f"{merge_tmp}^1", merge_tmp), "fetch_pull_head_merge_first_parent"
    except GitError:
        pass
    try:
        repo.run("fetch", "-q", "origin", f"pull/{pr}/head:{tmp}", "--deepen=200")
        merge_base = repo.run("merge-base", ref, tmp).strip()
        return git_numstat(repo, merge_base, tmp), "fetch_pull_head_merge_base_deepened"
    except GitError:
        pass
    try:
        return parse_numstat(repo.run("diff", "--numstat", f"{ref}...{tmp}")), "fetch_pull_head_three_dot"
    except GitError:
        pass
    try:
        return git_numstat(repo, ref, tmp), "fetch_pull_head_two_dot"
    except GitError:
        return None, "diff_failed"
