"""Shared fixtures: throwaway git repositories, and the target repo Scout analyzes."""

from __future__ import annotations

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


SCOUT_ROOT = Path(__file__).resolve().parents[1]
EPOCH_START = 1_600_000_000
DAY = 86_400
ALICE = ("Alice Example", "alice@example.com")
BOB = ("Bob Builder", "bob@example.com")
BOT = ("mssonicbld", "sonicbld@microsoft.com")


class RepoBuilder:
    """Builds a small history deterministically: every commit sits one day after the previous."""

    def __init__(self, path: Path):
        self.path = path
        self.clock = EPOCH_START
        path.mkdir(parents=True, exist_ok=True)
        self.environment = {
            key: value for key, value in os.environ.items() if not key.startswith("GIT_")
        }
        self.environment.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            HOME=str(path),
            LC_ALL="C",
        )
        self.git("init", "-q", "-b", "master")
        self.git("config", "core.autocrlf", "false")

    def git(self, *args: str, input: bytes | None = None, identity=ALICE, when: int | None = None) -> str:
        environment = dict(self.environment)
        stamp = f"{self.clock if when is None else when} +0000"
        environment.update(
            GIT_AUTHOR_NAME=identity[0],
            GIT_AUTHOR_EMAIL=identity[1],
            GIT_COMMITTER_NAME=identity[0],
            GIT_COMMITTER_EMAIL=identity[1],
            GIT_AUTHOR_DATE=stamp,
            GIT_COMMITTER_DATE=stamp,
        )
        result = subprocess.run(
            ["git", *args], cwd=self.path, env=environment, input=input, capture_output=True, check=True
        )
        return result.stdout.decode("utf-8", "replace").strip()

    def write(self, path: str, content: str | bytes, executable: bool = False) -> None:
        target = self.path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
        target.chmod(0o755 if executable else 0o644)

    def remove(self, path: str) -> None:
        (self.path / path).unlink()

    def commit(self, message: str, *, identity=ALICE, days: int = 1, stage_all: bool = True) -> str:
        self.clock += days * DAY
        if stage_all:
            self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "--no-verify", "-F", "-", input=message.encode(), identity=identity)
        return self.head()

    def gitlink(self, path: str, sha: str, name: str | None = None, url: str | None = None) -> None:
        """Stage a submodule pointer; the empty directory keeps later ``add -A`` calls from dropping it."""
        (self.path / path).mkdir(parents=True, exist_ok=True)
        if url:
            name = name or path
            self.git("config", "-f", ".gitmodules", f"submodule.{name}.path", path)
            self.git("config", "-f", ".gitmodules", f"submodule.{name}.url", url)
            self.git("add", ".gitmodules")
        self.git("update-index", "--add", "--cacheinfo", f"160000,{sha},{path}")

    def merge(self, branch: str, message: str, *, identity=ALICE) -> str:
        self.clock += DAY
        self.git("merge", "-q", "--no-ff", "--no-edit", "-m", message, branch, identity=identity)
        return self.head()

    def revert(self, sha: str, *, identity=ALICE) -> str:
        self.clock += DAY
        self.git("revert", "--no-edit", sha, identity=identity)
        return self.head()

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")


SWSS_OLD = "1" * 40
SWSS_NEW = "2" * 40


def build_history(path: Path) -> tuple[RepoBuilder, dict[str, str]]:
    repo = RepoBuilder(path)
    shas = {}
    repo.write("src/app/main.py", "".join(f"line {index}\n" for index in range(1, 21)))
    repo.write("README.md", "Maintained by Alice Example <alice@example.com>\n")
    repo.write("docs/notes.md", "notes\n")
    shas["root"] = repo.commit("Initial import")

    repo.write("src/app/main.py", "line 0\n" + "".join(f"line {index}\n" for index in range(1, 21)))
    repo.write("device/acme/x86_64-acme-r0/ACME-1/port_config.ini", "Ethernet0 0\n")
    shas["ordinary"] = repo.commit("[acme] Add ACME-1 (#2)\n\nAsked by Bob Builder.\n", identity=BOB)

    repo.remove("src/app/main.py")
    repo.write("src/app/core.py", "line 0\n" + "".join(f"line {index}\n" for index in range(1, 21)) + "tail\n")
    shas["rename"] = repo.commit("Rename main (#3)")

    repo.write("files/logo.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256)))
    shas["binary"] = repo.commit("Add logo")

    repo.write("src/app/core.py", (path / "src/app/core.py").read_text(), executable=True)
    shas["mode"] = repo.commit("Make core executable")

    repo.remove("docs/notes.md")
    shas["delete"] = repo.commit("Remove notes")

    repo.gitlink("src/sonic-swss", SWSS_OLD, name="src/sonic-swss", url="https://github.com/sonic-net/sonic-swss")
    shas["submodule_add"] = repo.commit("Add swss submodule", stage_all=False)

    repo.gitlink("src/sonic-swss", SWSS_NEW)
    shas["submodule_bump"] = repo.commit(
        "[submodule] Update submodule sonic-swss to the latest HEAD automatically\n\n"
        "#### Why I did it\nsrc/sonic-swss\n```\n"
        "* 1234567a - Fix x (#1) (2 hours ago) [Carol Stranger]\n```\n",
        identity=BOT,
        stage_all=False,
    )

    repo.git("checkout", "-q", "-b", "topic")
    repo.write("src/app/core.py", (path / "src/app/core.py").read_text() + "topic change\n", executable=True)
    repo.commit("Topic change", identity=BOB)
    repo.git("checkout", "-q", "master")
    repo.write("README.md", "Maintained by the team\n")
    shas["mainline"] = repo.commit("Tidy readme")
    shas["merge"] = repo.merge("topic", "Merge pull request #9 from bob/topic")

    shas["revert"] = repo.revert(shas["ordinary"])

    long_line = "x" * 3000
    repo.write("src/big.txt", "".join(f"row {index}\n" for index in range(1000)) + long_line + "\n")
    shas["big"] = repo.commit("Add big file")

    repo.write("docs/with space.md", "a\n")
    repo.write("docs/\u00fcn\u00ef.md", "b\n")
    repo.write('docs/q"uote.md', "c\n")
    repo.write("docs/tab\tname.md", "d\n")
    shas["paths"] = repo.commit("Odd names")
    return repo, shas


def mine_facts(path: Path, revision: str = "HEAD"):
    """Records, categories and facts of the first-parent history of ``revision``."""
    from scout_impl.dataset.categories import categorize
    from scout_impl.dataset.history import facts_from_record
    from scout_impl.mining.gitio import Git
    from scout_impl.mining.record import extract_records, make_context, record_to_dict

    git = Git(path)
    shas = git.first_parent_shas(revision)
    records = [record_to_dict(record) for record in extract_records(git, shas, make_context(git, revision))]
    facts = []
    landed = 0
    for index, record in enumerate(records):
        landed = max(landed, record["commit"]["committed_epoch"])
        facts.append(facts_from_record(index, record, categorize(record), landed))
    return git, records, facts


@pytest.fixture
def repo(tmp_path: Path) -> RepoBuilder:
    return RepoBuilder(tmp_path / "repo")
