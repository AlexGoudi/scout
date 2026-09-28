"""Shared fixtures: throwaway git repositories, and the target repo Scout analyzes."""

import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

from scout_impl.gitcmd import GitRepo

_AUTHOR_ENV = {
    "GIT_AUTHOR_NAME": "Scout Test",
    "GIT_AUTHOR_EMAIL": "scout@example.com",
    "GIT_COMMITTER_NAME": "Scout Test",
    "GIT_COMMITTER_EMAIL": "scout@example.com",
}

# Scout analyzes a separate sonic-mgmt checkout, so the tests that exercise real history
# need one supplied rather than derived from where Scout itself happens to sit.
TARGET_REPO_ENV = "SCOUT_TARGET_REPO"
# A sibling checkout, which is the layout you get by cloning both repos side by side.
DEFAULT_TARGET_REPO = Path(__file__).resolve().parents[2] / "sonic-mgmt"

# The suite is offline by default and that is a requirement, not a convenience (NFR-10),
# so the tests that fetch from a real remote are opt-in the same way the target repo is.
NETWORK_TESTS_ENV = "SCOUT_NETWORK_TESTS"
NETWORK_REMOTE_ENV = "SCOUT_NETWORK_REMOTE"
DEFAULT_NETWORK_REMOTE = "sonic-net/sonic-buildimage"


class TempGitRepo:
    """A small git working copy created for one test."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str, env: Optional[Dict[str, str]] = None) -> str:
        merged_env = dict(os.environ)
        merged_env.update(_AUTHOR_ENV)
        merged_env.update(env or {})
        completed = subprocess.run(
            ["git", "-c", "commit.gpgsign=false", *args],
            cwd=str(self.root),
            capture_output=True,
            text=True,
            env=merged_env,
        )
        if completed.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
        return completed.stdout

    def write(self, relpath: str, content: str) -> None:
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def remove(self, relpath: str) -> None:
        self.git("rm", "-q", relpath)

    def commit(self, message: str, date: Optional[str] = None, paths: Optional[List[str]] = None) -> str:
        self.git("add", "-A", *(paths or ["."]))
        env = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date} if date else None
        self.git("commit", "-q", "--allow-empty", "-m", message, env=env)
        return self.git("rev-parse", "HEAD").strip()

    def as_repo(self) -> GitRepo:
        return GitRepo(self.root)


@pytest.fixture
def isolated_git_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's own gitconfig out of the tests."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "gitconfig-system"))


@pytest.fixture
def temp_repo(tmp_path: Path, isolated_git_config: None) -> TempGitRepo:
    return TempGitRepo(tmp_path / "repo")


def _is_git_working_copy(path: Path) -> bool:
    return (path / ".git").exists()


def resolve_target_repo() -> Tuple[Optional[Path], str]:
    """Locate the sonic-mgmt checkout to analyze, or explain why there is none."""
    configured = os.environ.get(TARGET_REPO_ENV, "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if _is_git_working_copy(candidate):
            return candidate, ""
        return None, f"${TARGET_REPO_ENV} is set to {candidate}, which is not a git working copy"

    if _is_git_working_copy(DEFAULT_TARGET_REPO):
        return DEFAULT_TARGET_REPO, ""
    return None, (
        f"no sonic-mgmt working copy to analyze: set ${TARGET_REPO_ENV} to one, "
        f"or check out the default {DEFAULT_TARGET_REPO}"
    )


@pytest.fixture
def target_repo() -> Path:
    """The sonic-mgmt working copy under analysis, from $SCOUT_TARGET_REPO or the default."""
    root, reason = resolve_target_repo()
    if root is None:
        pytest.skip(reason)
    return root


@pytest.fixture
def network_remote() -> str:
    """The remote the integration tests fetch from, only when they are opted in to."""
    if os.environ.get(NETWORK_TESTS_ENV, "").strip().lower() not in ("1", "true", "yes"):
        pytest.skip(
            f"network tests are opt-in: set ${NETWORK_TESTS_ENV}=1 to fetch from a real remote "
            f"(and ${NETWORK_REMOTE_ENV} to use one other than {DEFAULT_NETWORK_REMOTE})"
        )
    return os.environ.get(NETWORK_REMOTE_ENV, "").strip() or DEFAULT_NETWORK_REMOTE


@pytest.fixture(scope="session")
def network_cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A throwaway cache root, so an integration run never touches the developer's own.

    Shared for the session, because sharing one is what the cache is for: only the
    first test that fetches a given pull request pays for it.
    """
    return tmp_path_factory.mktemp("scout-cache")
