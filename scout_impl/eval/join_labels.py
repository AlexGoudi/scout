"""Azure label join and the model-* calibration files."""

from __future__ import annotations

import collections
import hashlib
import json
import logging
import math
import os
import re
from pathlib import Path

from scout_impl.eval._cache import CODE_VERSION, Step, fingerprint
from scout_impl.eval._io import cache_exists, dump, load, progress
from scout_impl.gitcmd import GitRepo
from scout_impl.repos.base import RepoAdapter
from scout_impl.eval._pr_files import fetch_pr_files, files_for_model, path_bag_usable
from scout_impl.eval.azure_dumps import join_cfg
from scout_impl.eval.calibration_rules import (
    aggregate_attempt_labels,
    bucket_of,
    excluded_jobs,
    ext_of,
    gold_jobs,
    heuristic_p,
    is_submodule_sha_only,
)

logger = logging.getLogger(__name__)


def path_flag(path: str, spec: dict) -> bool:
    for x in spec.get("eq") or []:
        if path == x:
            return True
    for x in spec.get("contains") or []:
        if x in path:
            return True
    for x in spec.get("prefixes") or []:
        if path.startswith(x):
            return True
    return False


def features_from_files(files, cfg, priors, base):
    n_files = len(files)
    add = sum(f.get("additions") or 0 for f in files)
    dele = sum(f.get("deletions") or 0 for f in files)
    binary = sum(1 for f in files if f.get("binary"))
    buckets = collections.Counter()
    exts = collections.Counter()
    priors_hit = []
    bucket_priors = cfg.get("priors") or {}
    shared_name = cfg.get("shared_bucket_for_frac")
    n_shared = 0
    for f in files:
        p = f["path"]
        b = bucket_of(p, cfg)
        buckets[b] += 1
        exts[ext_of(p) or "(none)"] += 1
        priors_hit.append(priors.get(p, bucket_priors.get(b, base)))
        if b == shared_name:
            n_shared += 1
    max_prior = max(priors_hit) if priors_hit else base
    mean_prior = sum(priors_hit) / len(priors_hit) if priors_hit else base
    feat = {
        "n_files": n_files,
        "additions": add,
        "deletions": dele,
        "churn": add + dele,
        "binary_files": binary,
        "log1p_churn": round(math.log1p(add + dele), 4),
        "log1p_files": round(math.log1p(n_files), 4),
        "n_shared_paths": n_shared,
        "frac_shared": round(n_shared / n_files, 4) if n_files else 0.0,
        "max_revert_prior": round(max_prior, 6),
        "mean_revert_prior": round(mean_prior, 6),
        "buckets": dict(buckets),
        "extensions": dict(exts),
    }
    for name, prefix in (cfg.get("feature_prefix_counts") or {}).items():
        feat[name] = sum(1 for f in files if f["path"].startswith(prefix))
    for name, spec in (cfg.get("feature_path_flags") or {}).items():
        feat[name] = any(path_flag(f["path"], spec) for f in files)
    if "n_spytest_paths" in feat:
        feat["spytest_only"] = bool(n_files and feat["n_spytest_paths"] == n_files)
    feat["submodule_only"] = is_submodule_sha_only(files, cfg)
    feat["device_only"] = bool(n_files and feat.get("n_device_paths") == n_files)
    platforms = []
    for f in files:
        p = f["path"]
        if p.startswith("platform/") and "/" in p[len("platform/") :]:
            platforms.append(p.split("/")[1])
        if p.startswith("tests/") and "/" in p[len("tests/") :]:
            pass
    feat["platforms_touched"] = sorted(set(platforms))
    features_touched = []
    tests_prefix = (cfg.get("blast") or {}).get("tests_prefix")
    if tests_prefix:
        for f in files:
            p = f["path"]
            if p.startswith(tests_prefix):
                parts = p.split("/")
                if len(parts) >= 2:
                    features_touched.append(parts[1])
        feat["features_touched"] = sorted(set(features_touched))
    return feat


def build_labels(b, cfg):
    gold = gold_jobs(cfg)
    excl = excluded_jobs(cfg)
    labels = {}
    grouped = dict(b.get("groupJobs") or {})
    legacy = (cfg.get("jobs") or {}).get("legacy_timeline_fields") or {}
    for group, field in legacy.items():
        grouped.setdefault(group, {})
        grouped[group].update(b.get(field) or {})
    if b.get("pretestJobs"):
        grouped.setdefault("pretest", {}).update(b["pretestJobs"])
    if b.get("elastictestJobs"):
        grouped.setdefault("elastictest", {}).update(b["elastictestJobs"])
    if b.get("imageJobs"):
        grouped.setdefault("image", {}).update(b["imageJobs"])
    merged = {}
    for mp in grouped.values():
        merged.update(mp)
    for job in gold + excl:
        labels[job] = merged.get(job)
    return labels, grouped


def scan_pytest_markers(clone: str):
    mark_re = re.compile(
        r"pytest\.mark\.(\w+)(?:\((['\"])(.*?)\2\))?",
    )
    topo = collections.defaultdict(lambda: collections.Counter())
    files_n = 0
    tests_root = os.path.join(clone, "tests")
    if not os.path.isdir(tests_root):
        return {"n_files": 0, "by_file": [], "topology_counts": {}}
    for dirpath, dirnames, filenames in os.walk(tests_root):
        dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__"}]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, clone)
            try:
                text = open(path, errors="replace").read()
            except OSError:
                continue
            files_n += 1
            marks = mark_re.findall(text)
            topos = sorted({m[2] or m[0] for m in marks if m[0] == "topology" or m[0] == "topologies"})
            if "topology" in text or "pytest.mark" in text:
                for name, _, arg in marks:
                    if name in ("topology", "topologies") and arg:
                        for piece in re.split(r"[,\s]+", arg):
                            piece = piece.strip(" '\"")
                            if piece:
                                topo[rel][piece] += 1
            if topos:
                for t in topos:
                    topo[rel][t] += 1
    by_file = [
        {"path": p, "topologies": sorted(c.keys())}
        for p, c in sorted(topo.items())
        if c
    ]
    counts = collections.Counter()
    for rec in by_file:
        for t in rec["topologies"]:
            counts[t] += 1
    return {
        "n_python_files_scanned": files_n,
        "n_files_with_topology": len(by_file),
        "topology_file_counts": dict(counts.most_common()),
        "by_file": by_file[:4000],
        "note": "Static regex over tests/**/*.py; not a pytest collector.",
    }


def scan_docker_from(clone: str):
    from_re = re.compile(r"^\s*FROM\s+(\S+)", re.I | re.M)
    inc_re = re.compile(r"\{%\s*include\s+['\"]([^'\"]+)['\"]")
    roots = []
    for name in os.listdir(clone):
        p = os.path.join(clone, name)
        if os.path.isdir(p) and (name == "dockers" or name.startswith("sonic-slave")):
            roots.append(p)
    extra = os.path.join(clone, "files", "build_templates")
    if os.path.isdir(extra):
        roots.append(extra)
    rows = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            for fn in filenames:
                if "Dockerfile" not in fn:
                    continue
                path = os.path.join(dirpath, fn)
                rel = os.path.relpath(path, clone)
                try:
                    text = open(path, errors="replace").read()
                except OSError:
                    continue
                froms = from_re.findall(text)
                includes = inc_re.findall(text)
                apt = len(re.findall(r"\bapt-get\b|\bapt\b", text))
                if froms or includes or apt:
                    rows.append(
                        {
                            "path": rel,
                            "from": froms[:12],
                            "j2_includes": includes[:20],
                            "apt_mentions": apt,
                        }
                    )
    return {"n": len(rows), "files": rows, "note": "FROM / j2 include / apt mentions in Dockerfiles"}


JOIN_STEP = "join-labels"
JOIN_VERSION = "3"


def join_fingerprint(adapter: RepoAdapter, tip_sha: str, build_ids: list[int], cfg: dict) -> str:
    cfg_blob = hashlib.sha256(json.dumps(cfg, sort_keys=True, default=str).encode()).hexdigest()[:12]
    builds_blob = hashlib.sha256(json.dumps(sorted(build_ids)).encode()).hexdigest()[:12]
    return fingerprint(CODE_VERSION, JOIN_STEP, JOIN_VERSION, adapter.name, tip_sha[:12], builds_blob, cfg_blob)


def run_labels(
    adapter: RepoAdapter,
    out: str,
    history_dir: str | None = None,
    azure_dir: str | None = None,
    repo: GitRepo | None = None,
    resume: bool = True,
) -> None:
    if repo is None:
        raise ValueError("mine-labels needs a local git checkout")
    history_dir = history_dir or out
    azure_dir = azure_dir or out
    cfg = join_cfg(adapter)
    cfg = dict(cfg)
    cfg["github"] = {**cfg.get("github", {}), "clone": str(repo.root)}
    clone = cfg["github"]["clone"]
    ref = cfg["github"]["ref"]
    gold = gold_jobs(cfg)
    excl = excluded_jobs(cfg)
    groups = (cfg.get("jobs") or {}).get("groups") or {}
    is_image = "image" in groups
    pos_key = "prs_with_any_image_fail" if is_image else "prs_with_any_gold_fail"
    excl_note = (
        "Excluded jobs are contrast only, not y."
        if excl
        else "KVM / parent-pipeline jobs are not gold y."
    )
    parent_note = "parent pipeline result is not gold y"
    raw_note = (
        "Do not train by joining azure-pr-job-timelines.json yourself; "
        "the model-* files already did the join and dropped excluded jobs from y."
    )
    task = (
        "Predict whether a gold Azure job will fail from the PR file list (numstat). "
        f"Gold={gold}. Excluded={excl}. Not parent pipeline result, not reverts."
    )

    overview = load(history_dir, "git-overview.json")
    rates = load(history_dir, "git-file-revert-rates.json")
    commits = load(history_dir, "git-commit-file-changes.json")["commits"]
    timelines = load(azure_dir, "azure-pr-job-timelines.json")

    tip_sha = str(overview.get("tip_sha") or "")
    build_ids = [int(row["id"]) for row in timelines.get("builds") or [] if row.get("id") is not None]
    join_step = Step(Path(out), JOIN_STEP, join_fingerprint(adapter, tip_sha, build_ids, cfg), refresh=not resume)
    if (
        join_step.fresh()
        and cache_exists(out, "model-index.json")
        and cache_exists(out, "model-dataset-pr.json")
    ):
        logger.info("join-labels: cache fresh at tip %s (0 to process)", tip_sha[:12])
        return

    priors = {r["path"]: r["p_revert"] for r in rates.get("all_paths_n_ge_50") or []}
    base = rates["base_rate"]

    by_pr = {}
    for c in commits:
        if c.get("pr"):
            by_pr.setdefault(c["pr"], c)

    attempts_by_pr = collections.defaultdict(list)
    for b in timelines.get("builds") or []:
        if b.get("pr") is not None:
            attempts_by_pr[b["pr"]].append(b)
    pr_runs = {}
    for pr, attempts in attempts_by_pr.items():
        attempts.sort(key=lambda row: row.get("finishTime") or "")
        per_attempt = [build_labels(b, cfg) for b in attempts]
        labels_any = aggregate_attempt_labels([labels for labels, _ in per_attempt])
        grouped_any = {}
        for _, grouped in per_attempt:
            for group, mp in grouped.items():
                grouped_any.setdefault(group, []).append(mp)
        pr_runs[pr] = {
            "latest": attempts[-1],
            "attempt_ids": [b.get("id") or b.get("buildId") for b in attempts],
            "labels": labels_any,
            "labels_latest": per_attempt[-1][0],
            "grouped": {group: aggregate_attempt_labels(mps) for group, mps in grouped_any.items()},
            "fail_attempts": {
                job: sum(1 for labels, _ in per_attempt if labels.get(job) == "failed") for job in gold + excl
            },
        }

    dated = sorted(
        (pr for pr, run in pr_runs.items() if run["latest"].get("finishTime")),
        key=lambda pr: pr_runs[pr]["latest"]["finishTime"],
    )
    n = len(dated)
    n_train = int(n * 0.70)
    n_valid = int(n * 0.15)
    split_of = {}
    for i, pr in enumerate(dated):
        if i < n_train:
            split_of[pr] = "train"
        elif i < n_train + n_valid:
            split_of[pr] = "valid"
        else:
            split_of[pr] = "test"
    train_prs = [pr for pr in dated if split_of[pr] == "train"]
    valid_prs = [pr for pr in dated if split_of[pr] == "valid"]
    test_prs = [pr for pr in dated if split_of[pr] == "test"]

    job_n = collections.defaultdict(lambda: {"succeeded": 0, "failed": 0, "other": 0})
    job_n_train = collections.defaultdict(lambda: {"succeeded": 0, "failed": 0})
    for pr, run in pr_runs.items():
        for job, res in run["labels"].items():
            if res in ("succeeded", "failed"):
                job_n[job][res] += 1
                if split_of.get(pr, "train") == "train":
                    job_n_train[job][res] += 1
            elif res:
                job_n[job]["other"] += 1
    job_base = {}
    for job in gold + excl:
        s, f = job_n_train[job]["succeeded"], job_n_train[job]["failed"]
        tot = s + f
        job_base[job] = (f / tot) if tot else base

    path_job = collections.defaultdict(lambda: {"n": 0, "fail": 0})
    for pr, run in pr_runs.items():
        if split_of.get(pr, "train") != "train":
            continue
        labels = run["labels"]
        files = by_pr[pr]["files"] if pr in by_pr else None
        if not files:
            continue
        for f in files:
            for job in gold:
                res = labels.get(job)
                if res not in ("succeeded", "failed"):
                    continue
                rec = path_job[(f["path"], job)]
                rec["n"] += 1
                if res == "failed":
                    rec["fail"] += 1
    path_job_rows = []
    for (path, job), rec in path_job.items():
        if rec["n"] < 5:
            continue
        path_job_rows.append(
            {
                "path": path,
                "job": job,
                "n": rec["n"],
                "fail": rec["fail"],
                "p_fail": round(rec["fail"] / rec["n"], 4),
            }
        )
    path_job_rows.sort(key=lambda x: (-x["p_fail"], -x["n"]))

    dump(
        out,
        "model-azure-path-job-fail.json",
        {
            "n_timelines": timelines.get("n"),
            "n_prs": len(pr_runs),
            "label_rule": "per PR: failed if any attempt failed",
            "job_outcome_counts": {k: dict(v) for k, v in job_n.items()},
            "job_fail_rate": {k: round(v, 4) for k, v in job_base.items()},
            "job_fail_rate_source": "train split PRs only",
            "note": excl_note,
            "min_n": 5,
            "rows": path_job_rows[:400],
        },
    )

    prs = []
    unresolved = []
    fetched = 0
    merge_base_n = 0
    pull_merge_n = 0
    two_dot_n = 0
    deepen_n = 0
    runs = sorted(pr_runs.items(), key=lambda item: item[1]["latest"].get("finishTime") or "", reverse=True)
    n_fetch = sum(1 for pr, _ in runs if pr not in by_pr)
    fetch_i = 0
    for pr, run in runs:
        b = run["latest"]
        files = None
        diff_source = None
        sha = None
        if pr in by_pr:
            files = by_pr[pr]["files"]
            sha = by_pr[pr]["sha"]
            diff_source = "origin/master_squash_numstat"
        else:
            fetch_i += 1
            files, diff_source = fetch_pr_files(repo, ref, pr)
            progress(fetch_i, n_fetch, "PR fetch", every=25, unit="PRs")
            if files is not None:
                fetched += 1
                sha = b.get("sourceVersion")
                if diff_source and "merge_base" in diff_source:
                    merge_base_n += 1
                if diff_source and "pull_merge" in diff_source:
                    pull_merge_n += 1
                if diff_source and "deepened" in diff_source:
                    deepen_n += 1
                if diff_source and "two_dot" in diff_source:
                    two_dot_n += 1
        if files is None:
            unresolved.append(pr)
            files = []
            diff_source = diff_source or "unresolved"
            sha = b.get("sourceVersion")
        labels, grouped = run["labels"], run["grouped"]
        feat = features_from_files(files, cfg, priors, base)
        model_files = files_for_model(files, diff_source)
        model_feat = features_from_files(model_files, cfg, priors, base)
        heuristic = {
            job: heuristic_p(
                model_files, job, cfg, priors, base, job_base.get(job, base)
            )
            for job in gold
        }
        failed_gold = [j for j in gold if labels.get(j) == "failed"]
        rec = {
            "pr": pr,
            "sha": sha,
            "azureBuildId": b.get("id") or b.get("buildId"),
            "azureBuildIds": run["attempt_ids"],
            "n_attempts": len(run["attempt_ids"]),
            "finishTime": b.get("finishTime"),
            "startTime": b.get("startTime"),
            "diff_source": diff_source,
            "path_bag_usable": path_bag_usable(diff_source),
            "pipelineResult": b.get("result") or b.get("pipelineResult"),
            "features": feat,
            "model_features": model_feat,
            "heuristic_p_job": heuristic,
            "labels": labels,
            "labels_latest": run["labels_latest"],
            "fail_attempts": run["fail_attempts"],
            "failedGoldJobs": failed_gold,
            "nFailedGoldJobs": len(failed_gold),
            "files": [
                {
                    "path": f["path"],
                    "additions": f.get("additions"),
                    "deletions": f.get("deletions"),
                    "binary": f.get("binary", False),
                }
                for f in files
            ],
            "totals": {
                "files": feat["n_files"],
                "additions": feat["additions"],
                "deletions": feat["deletions"],
                "churn": feat["churn"],
                "binary_files": feat["binary_files"],
            },
        }
        if grouped.get("image"):
            rec["failedImageJobs"] = [j for j, r in grouped["image"].items() if r == "failed"]
        rec["split"] = split_of.get(pr, "train")
        prs.append(rec)

    dump(
        out,
        "azure-pr-file-changes.json",
        {
            "schema": {
                "files": ["path", "additions", "deletions", "binary"],
                "join": "Azure PR timelines (every fetched attempt per PR; labels failed if any attempt failed) + git file numstat of the final diff",
                "diff_source": "origin/master_squash_numstat if merged; else pull/N/head vs merge-base; else pull/N/merge^1; else deepen head and retry merge-base; else three/two-dot leftover",
                "note": "paths and line counts only, not hunks. two-dot bags stay here; model-* empties them",
            },
            "n_prs": len(prs),
            "n_with_files": sum(1 for p in prs if p["files"]),
            "n_unresolved": len(unresolved),
            "unresolved_prs": unresolved,
            "n_fetched_open": fetched,
            "n_merge_base": merge_base_n,
            "n_pull_merge": pull_merge_n,
            "n_deepened": deepen_n,
            "n_two_dot": two_dot_n,
            "n_file_rows": sum(len(p["files"]) for p in prs),
            "prs": prs,
        },
    )

    examples = []
    jsonl = []
    n_pos_pr = 0
    n_pos_job = 0
    n_job_rows = 0
    n_canceled = 0
    n_missing = 0
    for p in prs:
        if p["nFailedGoldJobs"] > 0:
            n_pos_pr += 1
        gold_labels = {j: p["labels"].get(j) for j in gold}
        excl_labels = {j: p["labels"].get(j) for j in excl}
        examples.append(
            {
                "pr": p["pr"],
                "sha": p["sha"],
                "azureBuildId": p["azureBuildId"],
                "finishTime": p["finishTime"],
                "startTime": p["startTime"],
                "diff_source": p["diff_source"],
                "path_bag_usable": p["path_bag_usable"],
                "pipelineResult": p["pipelineResult"],
                "features": p["model_features"],
                "heuristic_p_job": p["heuristic_p_job"],
                "labels": gold_labels,
                "labels_latest": {j: p["labels_latest"].get(j) for j in gold},
                "n_attempts": p["n_attempts"],
                "vpp_labels": excl_labels if excl else {},
                "excluded_labels": excl_labels,
                "split": p["split"],
                "paths": [f["path"] for f in files_for_model(p["files"], p["diff_source"])],
                "files": files_for_model(p["files"], p["diff_source"]),
            }
        )
        for job in gold:
            yraw = p["labels"].get(job)
            if yraw is None:
                n_missing += 1
                continue
            if yraw not in ("succeeded", "failed"):
                n_canceled += 1
                continue
            y = 1 if yraw == "failed" else 0
            n_job_rows += 1
            n_pos_job += y
            jsonl.append(
                {
                    "pr": p["pr"],
                    "sha": p["sha"],
                    "azureBuildId": p["azureBuildId"],
                    "finishTime": p["finishTime"],
                    "split": p["split"],
                    "job": job,
                    "y": y,
                    "y_latest": 1 if p["labels_latest"].get(job) == "failed" else 0,
                    "n_attempts": p["n_attempts"],
                    "fail_attempts": p["fail_attempts"].get(job, 0),
                    "heuristic_p": p["heuristic_p_job"].get(job, base),
                    "features": p["model_features"],
                    "path_bag_usable": p["path_bag_usable"],
                }
            )

    class_balance = {
        "prs": len(prs),
        pos_key: n_pos_pr,
        "prs_with_any_gold_fail": n_pos_pr,
        "pr_job_rows": n_job_rows,
        "pr_job_positives": n_pos_job,
        "positive_rate_pr": round(n_pos_pr / len(prs), 4) if prs else 0,
        "positive_rate_job": round(n_pos_job / n_job_rows, 4) if n_job_rows else 0,
        "canceled_or_missing_job_slots": {"canceled": n_canceled, "missing": n_missing},
    }
    if pos_key == "prs_with_any_image_fail":
        class_balance["prs_with_any_image_fail"] = n_pos_pr

    dump(
        out,
        "model-dataset-pr.json",
        {
            "schema_ref": "model-schema.json",
            "n": len(examples),
            "jobs": gold,
            "excluded_jobs": excl,
            "class_balance": class_balance,
            "examples": examples,
        },
    )
    dump(out, "model-dataset-pr-job.jsonl", jsonl, indent=None)

    dump(
        out,
        "model-splits.json",
        {
            "method": "chronological by Azure finishTime, 70/15/15 by PR (not by job row)",
            "train_prs": train_prs,
            "valid_prs": valid_prs,
            "test_prs": test_prs,
            "counts": {"train": len(train_prs), "valid": len(valid_prs), "test": len(test_prs)},
            "n_train": len(train_prs),
            "n_valid": len(valid_prs),
            "n_test": len(test_prs),
        },
    )
    dump(
        out,
        "model-path-priors.json",
        {
            "base_rate_revert": base,
            "by_path_n_ge_50": {r["path"]: r["p_revert"] for r in rates.get("all_paths_n_ge_50") or []},
            "bucket_priors": cfg.get("priors") or {},
            "jobs": gold,
        },
    )
    dump(
        out,
        "model-schema.json",
        {
            "task": task,
            "target": {
                "y": "1 if that gold job failed on any fetched attempt of the PR, else 0",
                "y_latest": "1 if it failed on the latest attempt only; hides failures fixed before merge",
                "heuristic_job_base": "per-job fail rate from train-split PRs only",
                "drop": "canceled/missing jobs omitted from pr-job rows; labels[job]=null on PR records",
                "do_not_use": [
                    "pipelineResult",
                    *[f"{j} labels" for j in excl],
                    "author / vendor identity",
                    "GitHub Actions Semgrep/DCO/CodeQL",
                    "automerge label",
                ],
            },
            "recommended_input_file": "model-dataset-pr.json",
            "sklearn_flat_file": "model-dataset-pr-job.jsonl",
            "feature_notes": {
                "heuristic_p_job / heuristic_p": "phase-0 rules baseline; a model should beat this",
                "max_revert_prior": "from historical reverts, weak proxy",
                "paths/files": "bag of paths + add/del; do not treat line count as the main signal. empty when path_bag_usable is false",
                "path_bag_usable": "false for leftover two-dot diffs; Azure labels stay; do not fill paths from azure-pr-file-changes",
                "split": "attached on each example; split by PR so all jobs of a PR stay together",
            },
            "class_balance": class_balance,
            "leakage": "Do not train on test_prs. Do not use future nightlies as PR features.",
            "how_to_feed": {
                "gradient_boosted_trees": "Load model-dataset-pr-job.jsonl. Flatten features.* + job one-hot + heuristic_p -> y. Ignore pipelineResult.",
                "path_bag_or_code_model": "Load model-dataset-pr.json. Use paths/files as input, labels[job] as multi-task targets. Repeat the same split field. Do not restore two-dot files from azure-pr-file-changes.json.",
                "baseline": "heuristic_p / heuristic_p_job is the rules scorer. Report AUC/AP vs this, not vs random only.",
                "raw_dumps": raw_note,
            },
        },
    )

    scan = cfg.get("scan") or {}
    still_missing = []
    if scan.get("pytest_markers"):
        markers = scan_pytest_markers(clone)
        dump(out, "repo-pytest-markers.json", markers)
    else:
        still_missing.append("pytest marker / topology tags not scanned for this repo")
    if scan.get("docker_from"):
        dump(out, "repo-docker-from.json", scan_docker_from(clone))
    if two_dot_n:
        still_missing.append(
            f"{two_dot_n} PRs still two-dot vs {ref}; stripped from model-* path bags"
        )
    if n_pos_pr < 20:
        still_missing.append("More labeled PRs (positives are still rare)")
    still_missing.append("Full Azure job log stage text (collect vs DUT vs traffic / configure vs package)")

    dump(
        out,
        "model-index.json",
        {
            "purpose": "What to feed a model. Raw dumps stay in sibling JSON files; these are the joined examples.",
            "train_on": {
                "model-dataset-pr.json": f"{len(examples)} PRs: paths, numstat files, scalar features, heuristic_p_job, labels per gold job, split",
                "model-dataset-pr-job.jsonl": f"{n_job_rows} rows (pr, job) -> y. Same features, no duplicated path lists. For trees / sklearn.",
            },
            "lookups": {
                "model-schema.json": "target definition, leakage, how_to_feed",
                "model-splits.json": "chronological 70/15/15 by PR",
                "model-path-priors.json": "historical revert p(file) n>=50 plus bucket priors",
                "model-azure-path-job-fail.json": "small-n P(job fail) on these PRs, including excluded jobs for contrast",
            },
            "do_not_train_on_directly": [
                "git-commit-file-changes.json — unlabeled / revert-era",
                "azure-pr-job-timelines.json — includes excluded jobs",
                f"pipelineResult on any record — {parent_note}",
            ],
            "class_balance": class_balance,
            "still_missing": still_missing,
            "layout_recommendation": "Keep raw dumps separate. Merge only into model-dataset-* for training.",
        },
    )

    top_files = []
    for r in (rates.get("all_paths_n_ge_50") or [])[:12]:
        top_files.append(
            {
                "path": r["path"],
                "p": r["p_revert"],
                "lift": r.get("lift_vs_base"),
                "bucket": r.get("bucket") or bucket_of(r["path"], cfg),
            }
        )
    dump(
        out,
        "scoring-plan.json",
        {
            "path_weights": {
                "source": "git-file-revert-rates.json empirical p_revert, n>=50 where possible",
                "files": top_files,
                "file_priors": priors,
                "base_rate_revert": base,
                "bucket_priors": cfg.get("priors") or {},
            },
            "job_base": {job: round(job_base.get(job, base), 6) for job in gold},
            "job_base_source": "Azure gold-job fail rate over train-split PRs only",
            "prefix_map": {
                "all_gold_jobs": (cfg.get("paths") or {}).get("high_risk", {}).get("prefixes", []),
                "feature_to_jobs": (cfg.get("blast") or {}).get("feature_to_jobs") or {},
                "platform_to_jobs": (cfg.get("blast") or {}).get("platform_to_jobs") or {},
            },
            "plan": {
                "gold_jobs": gold,
                "excluded_jobs": excl,
                "cli_contract": {
                    "cmd": "score-pr --base origin/master",
                    "diff": "git diff --name-only <base>...HEAD",
                    "scorer": "calibration_rules.heuristic_p, identical to model-* heuristic_p_job",
                    "stdout": {"score_by_job": {}, "files": [], "reasons": []},
                },
            },
        },
    )

    join_step.commit(
        [
            "model-index.json",
            "model-dataset-pr.json",
            "model-dataset-pr-job.jsonl",
            "scoring-plan.json",
        ],
        {"tip_sha": tip_sha, "build_ids": build_ids},
    )

    logger.info(
        "join done %s class_balance=%s unresolved=%d fetched=%d merge_base=%d pull_merge=%d deepened=%d two_dot=%d",
        overview.get("repo"),
        class_balance,
        len(unresolved),
        fetched,
        merge_base_n,
        pull_merge_n,
        deepen_n,
        two_dot_n,
    )
