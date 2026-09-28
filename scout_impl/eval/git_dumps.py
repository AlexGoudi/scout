"""Git log / numstat dumps for calibration (local checkout)."""

from __future__ import annotations

import collections
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path

from ..gitcmd import GitRepo
from ..incidents import read_corpus
from ..mining.gitio import parse_numstat_text
from ..repos.base import RepoAdapter
from ._cache import CODE_VERSION, Step, fingerprint
from ._io import dump, now_iso, progress, try_load
from .azure_dumps import join_cfg
from .calibration_rules import bucket_of, is_headline_revert, last_pr
from .incidents_cache import INCIDENTS_FILE, run_incidents_cache

logger = logging.getLogger(__name__)

GIT_DUMP_MARKERS = (
    "git-overview.json",
    "git-commit-file-changes.json",
    "git-file-revert-rates.json",
)
STEP_NAME = "git-dumps"


def git_dumps_fingerprint(adapter: RepoAdapter, ref: str) -> str:
    cfg = join_cfg(adapter)
    blob = json.dumps(cfg.get("buckets") or cfg.get("paths") or cfg, sort_keys=True, default=str)
    return fingerprint(CODE_VERSION, STEP_NAME, adapter.name, ref, hashlib.sha256(blob.encode()).hexdigest()[:12])


def run_git_dumps(
    adapter: RepoAdapter,
    repo: GitRepo,
    out_dir: str,
    ref: str = "origin/master",
    collected: str | None = None,
    resume: bool = True,
) -> str:
    """Write git-* calibration dumps incrementally; returns resolved tip sha."""
    collected = collected or now_iso()
    cfg = join_cfg(adapter)
    tip = repo.rev_parse(ref)
    directory = Path(out_dir)
    step = Step(directory, STEP_NAME, git_dumps_fingerprint(adapter, ref), refresh=not resume)

    run_incidents_cache(repo, directory, revision=ref, refresh=not resume)
    incidents = read_corpus(directory / INCIDENTS_FILE)
    revert_shas = {item.revert_sha for item in incidents}
    revert_count = len(incidents)

    prev = (step.cursor or {}) if step.fresh() else {}
    prev_tip = prev.get("tip_sha")
    existing_commits: list[dict] = []
    if prev_tip and resume and try_load(out_dir, "git-commit-file-changes.json"):
        payload = try_load(out_dir, "git-commit-file-changes.json") or {}
        existing_commits = list(payload.get("commits") or [])

    if resume and step.fresh() and prev.get("tip_sha") == tip and existing_commits:
        logger.info("git-dumps: cache fresh at %s (0 commits to process)", tip[:12])
        return tip

    if not resume or not prev_tip or not repo.is_ancestor(prev_tip, tip):
        if prev_tip and resume and not repo.is_ancestor(prev_tip, tip):
            logger.warning("git-dumps: %s not ancestor of %s; full re-walk", prev_tip[:12], tip[:12])
        existing_commits = []
        walk_spec = ref
    else:
        walk_spec = f"{prev_tip}..{tip}"

    subject = repo.run("log", "-1", "--format=%s", tip).strip()
    count_line = repo.run("rev-list", "--count", ref).strip()
    commit_count = int(count_line)
    dates = (
        repo.run("log", ref, "--reverse", "--format=%aI", "-1").strip(),
        repo.run("log", ref, "-1", "--format=%aI").strip(),
    )
    gh = cfg.get("github") or {}
    overview = {
        "ref": ref,
        "repo": f"github.com/{gh.get('owner', 'sonic-net')}/{gh.get('repo', adapter.name)}",
        "commit_count": commit_count,
        "first_date": dates[0][:10] if dates[0] else "",
        "last_date": dates[1][:10] if dates[1] else "",
        "tip_sha": tip,
        "tip_subject": subject,
        "revert_count": revert_count,
        "revert_rate_pct": round(100.0 * revert_count / commit_count, 4) if commit_count else 0.0,
        "collected_at": collected,
    }
    dump(out_dir, "git-overview.json", overview)

    by_sha = {row["sha"]: row for row in existing_commits}
    log_format = "%H%x1f%aI%x1f%s"
    entries = repo.run("log", walk_spec, f"--format={log_format}", "--no-merges").splitlines()
    total = len(entries)
    logger.info("Walking %d commit(s) on %s", total, walk_spec)
    for index, line in enumerate(entries, 1):
        if "\x1f" not in line:
            continue
        sha, authored, subject_line = line.split("\x1f", 2)
        pr = last_pr(subject_line)
        if pr is None:
            continue
        try:
            numstat = repo.run("diff", "--numstat", f"{sha}^", sha)
        except Exception:
            continue
        files = parse_numstat_text(numstat)
        if not files:
            continue
        is_revert = is_headline_revert(subject_line) or sha in revert_shas
        row = {
            "sha": sha,
            "ts": int(datetime.fromisoformat(authored.replace("Z", "+00:00")).timestamp()),
            "subject": subject_line,
            "pr": pr,
            "is_revert": is_revert,
            "files": files,
        }
        by_sha[sha] = row
        progress(index, total, "git numstat", every=250, unit="commits")

    commits = sorted(by_sha.values(), key=lambda item: item["ts"])
    path_touches = collections.Counter()
    path_reverts = collections.Counter()
    for row in commits:
        for file_row in row.get("files") or []:
            path = file_row["path"]
            path_touches[path] += 1
            if row.get("is_revert"):
                path_reverts[path] += 1
    dump(
        out_dir,
        "git-commit-file-changes.json",
        {
            "ref": ref,
            "schema": {
                "files": ["path", "additions", "deletions", "binary"],
                "pr": "last (#N) in subject, squash-merge convention",
            },
            "n_commits": commit_count,
            "n_with_files": len(commits),
            "n_file_rows": sum(len(item["files"]) for item in commits),
            "collected_at": collected,
            "commits": commits,
        },
    )

    base_rate = revert_count / commit_count if commit_count else 0.0
    rows = []
    for path, touches in path_touches.items():
        if touches < 50:
            continue
        reverts_n = path_reverts.get(path, 0)
        rate = reverts_n / touches
        rows.append(
            {
                "path": path,
                "touches": touches,
                "reverts": reverts_n,
                "p_revert": round(rate, 6),
                "p_revert_pct": round(rate * 100, 3),
                "lift_vs_base": round(rate / base_rate, 3) if base_rate else 0.0,
                "bucket": bucket_of(path, cfg),
            }
        )
    rows.sort(key=lambda item: (-item["p_revert"], -item["touches"]))
    dump(
        out_dir,
        "git-file-revert-rates.json",
        {
            "base_rate": base_rate,
            "base_rate_pct": round(base_rate * 100, 4),
            "note": "p_revert = reverts/touches for commits that touched the path",
            "all_paths_n_ge_50": rows,
        },
    )
    step.commit(list(GIT_DUMP_MARKERS) + [INCIDENTS_FILE], {"tip_sha": tip, "ref": ref})
    return tip
