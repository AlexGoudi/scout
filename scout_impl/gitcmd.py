"""Read-only git wrapper used by ingest and the incident miner."""

import logging
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Pinned so output shape does not depend on the caller's gitconfig (NFR-3). Every git
# invocation Scout makes, local or against the remote cache, carries these.
CONFIG_OVERRIDES = (
    "-c", "core.quotepath=false",
    "-c", "diff.noprefix=false",
    "-c", "diff.mnemonicPrefix=false",
    "-c", "i18n.logOutputEncoding=utf-8",
    "-c", "log.showSignature=false",
)

EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class GitError(RuntimeError):
    """A git invocation exited non-zero."""


class GitRepo:
    """Runs read-only git commands in a working copy."""

    def __init__(self, root: Path, timeout_seconds: int = 300) -> None:
        self.root = Path(root).resolve()
        self.timeout_seconds = timeout_seconds

    def run(self, *args: str, check: bool = True, stdin: Optional[str] = None) -> str:
        command = ["git", *CONFIG_OVERRIDES, *args]
        logger.debug("Running %s in %s", " ".join(args), self.root)
        completed = subprocess.run(
            command,
            cwd=str(self.root),
            input=stdin,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=self.timeout_seconds,
        )
        if check and completed.returncode != 0:
            raise GitError(
                f"git {' '.join(args)} failed with {completed.returncode}: {completed.stderr.strip()}"
            )
        return completed.stdout

    def rev_parse(self, revision: str) -> str:
        """Resolve a revision to a full commit sha, raising if it does not exist."""
        resolved = self.run("rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}", check=False).strip()
        if not resolved:
            raise GitError(f"Revision not found: {revision}")
        return resolved

    def resolve_commits(self, revisions: List[str]) -> Dict[str, str]:
        """Best-effort batch resolution; revisions absent from the clone are omitted."""
        if not revisions:
            return {}

        stdin = "".join(f"{revision}^{{commit}}\n" for revision in revisions)
        output = self.run("cat-file", "--batch-check", stdin=stdin, check=False)
        resolved: Dict[str, str] = {}
        for revision, line in zip(revisions, output.splitlines()):
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "commit":
                resolved[revision] = parts[0]
        return resolved

    def merge_base(self, left: str, right: str) -> str:
        output = self.run("merge-base", left, right).strip()
        if not output:
            raise GitError(f"No merge base between {left} and {right}")
        return output
