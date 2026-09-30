"""Phase-0 score-pr: pure git diff + adapter calibration tables."""

from __future__ import annotations

import json
from pathlib import Path

from ..gitcmd import GitError, GitRepo
from ..repos.base import RepoAdapter
from .azure_dumps import join_cfg
from .calibration_rules import bucket_of, heuristic_p, jobs_for_path


def _load_plan(calibration_dir: Path | None) -> tuple[dict, str]:
    if calibration_dir is None:
        return {}, "adapter_defaults"
    plan_path = calibration_dir / "scoring-plan.json"
    if not plan_path.is_file():
        return {}, "adapter_defaults"
    with plan_path.open(encoding="utf-8") as handle:
        return json.load(handle), "scoring-plan"


def score_paths(
    paths: list[str],
    adapter: RepoAdapter,
    base_ref: str = "origin/master",
    calibration_dir: Path | None = None,
) -> dict:
    cfg = join_cfg(adapter)
    plan, source = _load_plan(calibration_dir)
    weights = plan.get("path_weights") or {}
    file_priors = weights.get("file_priors")
    if file_priors is None:
        file_priors = {row["path"]: row["p"] for row in weights.get("files") or []}
    bucket_priors = cfg.get("priors") or {}
    base = float(weights.get("base_rate_revert") or plan.get("base_rate_revert") or cfg.get("base_rate") or 0.012)
    gold = (plan.get("plan") or {}).get("gold_jobs") or (cfg.get("jobs") or {}).get("gold") or []
    job_base = plan.get("job_base") or {}
    files = [{"path": path} for path in paths]
    score_by_job = {
        job: heuristic_p(files, job, cfg, file_priors, base, float(job_base.get(job, base))) for job in gold
    }
    reasons = []
    for path in paths:
        bucket = bucket_of(path, cfg)
        reasons.append(
            {
                "path": path,
                "bucket": bucket,
                "path_class": adapter.classify(path).qualified_id,
                "prior": file_priors.get(path, bucket_priors.get(bucket, base)),
                "jobs": [job for job in jobs_for_path(path, cfg) if job in score_by_job],
            }
        )
    return {
        "base_ref": base_ref,
        "score_by_job": score_by_job,
        "job_base_source": "scoring-plan" if job_base else "revert_base_rate",
        "files": paths,
        "reasons": reasons,
        "source": source,
    }


def changed_paths(repo: GitRepo, base_ref: str, head_ref: str = "HEAD") -> list[str]:
    """Files the change touches since it forked from `base_ref`, as the PR diff shows them."""
    raw = repo.run("diff", "--name-only", f"{base_ref}...{head_ref}")
    return [path for path in raw.splitlines() if path.strip()]


def score_pr_repo(
    repo: GitRepo,
    adapter: RepoAdapter,
    base_ref: str,
    calibration_dir: Path | None = None,
) -> dict:
    paths = changed_paths(repo, base_ref)
    return score_paths(paths, adapter, base_ref=base_ref, calibration_dir=calibration_dir)


def score_from_brief(
    brief_path: str,
    adapter: RepoAdapter,
    calibration_dir: Path | None = None,
    repo: GitRepo | None = None,
) -> dict:
    """Score a brief's change. With a checkout, every changed file; without, the capped hotspots."""
    with open(brief_path, encoding="utf-8") as handle:
        brief = json.load(handle)
    header = brief.get("brief") or {}
    base_sha, head_sha = header.get("base_sha"), header.get("head_sha")
    paths = None
    if repo is not None and base_sha and head_sha:
        try:
            paths = changed_paths(repo, base_sha, head_sha)
        except GitError:
            paths = None
    complete = paths is not None
    if paths is None:
        paths = [item.get("path") for item in brief.get("hotspots") or [] if item.get("path")]
    out = score_paths(paths, adapter, base_ref=base_sha or "", calibration_dir=calibration_dir)
    out["source"] = "scout-brief-view"
    out["brief_path"] = brief_path
    out["files_complete"] = complete
    return out
