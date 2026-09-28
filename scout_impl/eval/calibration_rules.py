#!/usr/bin/env python3
"""Path buckets, blast-radius, revert helpers — driven by adapter JSON."""

from __future__ import annotations

import re

from ..mining.message import PR_IN_PARENS_RE
TAG_RE = re.compile(r"^\[.*?\]\s*")
PREFIX_RE = re.compile(r"^\[([^\]]+)\]")


def is_revert(subject: str) -> bool:
    s = subject.strip()
    if s.startswith("Revert"):
        return True
    while True:
        m = TAG_RE.match(s)
        if not m:
            break
        s = s[m.end() :]
    return s.startswith("Revert")


def is_headline_revert(subject: str) -> bool:
    """HLD / git log --grep='^Revert' — subject must start with Revert (no leading tag)."""
    return subject.strip().startswith("Revert")


def last_pr(subject: str):
    found = PR_IN_PARENS_RE.findall(subject)
    return int(found[-1]) if found else None


def ext_of(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    if "." not in base or base.startswith("."):
        return ""
    return base.rsplit(".", 1)[-1].lower()


def _match_rule(path: str, rule: dict) -> bool:
    if path in rule.get("exact", []):
        return True
    for p in rule.get("prefixes", []):
        if path.startswith(p):
            return True
    for s in rule.get("contains", []):
        if s in path:
            return True
    for rx in rule.get("regex", []):
        if re.search(rx, path):
            return True
    return False


def bucket_of(path: str, cfg: dict) -> str:
    paths = cfg.get("paths") or {}
    for rule in paths.get("buckets", []):
        if _match_rule(path, rule):
            return rule["name"]
    return paths.get("default_bucket", "other")


def gold_jobs(cfg: dict) -> list[str]:
    return list((cfg.get("jobs") or {}).get("gold") or [])


def excluded_jobs(cfg: dict) -> list[str]:
    return list((cfg.get("jobs") or {}).get("excluded") or [])


def pretest_jobs(cfg: dict) -> list[str]:
    return list((cfg.get("jobs") or {}).get("pretest") or [])


def jobs_for_path(path: str, cfg: dict) -> list[str]:
    blast = cfg.get("blast") or {}
    gold = gold_jobs(cfg)
    pre = pretest_jobs(cfg)
    b = bucket_of(path, cfg)
    if b in set(blast.get("all_gold_buckets") or []):
        return gold[:]
    bucket_jobs = (blast.get("bucket_jobs") or {}).get(b)
    if bucket_jobs is not None:
        return _expand_job_list(bucket_jobs, pre, gold)
    tests_prefix = blast.get("tests_prefix")
    if tests_prefix and path.startswith(tests_prefix):
        feat_i = int(blast.get("feature_segment", 1))
        parts = path.split("/")
        feat = parts[feat_i] if len(parts) > feat_i else ""
        mapped = (blast.get("feature_to_jobs") or {}).get(feat)
        if mapped:
            return _expand_job_list(["pretest"] + list(mapped), pre, gold)
        default_feat = blast.get("unknown_feature_jobs", "pretest_plus_gold")
        if default_feat == "pretest_plus_gold":
            return _uniq(pre + gold)
        if default_feat == "pretest":
            return pre[:]
        return gold[:]
    platform_prefix = blast.get("platform_prefix")
    if platform_prefix and path.startswith(platform_prefix):
        vendor = path.split("/")[1] if "/" in path else ""
        vendor_map = blast.get("platform_to_jobs") or {}
        if vendor in vendor_map:
            return _expand_job_list(vendor_map[vendor], pre, gold)
        key = vendor.replace("-", "_")
        if key in gold:
            return _uniq(pre + [key])
    device_prefix = blast.get("device_prefix")
    if device_prefix and path.startswith(device_prefix):
        vendor = path.split("/")[1] if "/" in path else ""
        vendor_map = blast.get("device_to_jobs") or {}
        if vendor in vendor_map:
            return _expand_job_list(vendor_map[vendor], pre, gold)
        key = vendor.replace("-", "_")
        if key in gold:
            return _uniq(pre + [key])
    return pre[:] if pre else []


def heuristic_p(files, job, cfg, priors, base, job_base):
    """Phase-0 rules score for one gold job: shared by `score-pr` and `model-*` `heuristic_p_job`.

    `job_base` is P(job fails) and path priors are P(revert | path), two different scales, so a
    path contributes its revert lift (prior / `base`, the overall revert rate) applied to the
    job's base rate. The riskiest path reaching the job sets the score; paths never lower it.
    """
    h = cfg.get("heuristic") or {}
    cap = float(h.get("cap") or 0.35)
    if not files or base <= 0:
        return round(min(job_base, cap), 6)
    lift = 1.0
    skip_sub = h.get("skip_mark_substr") or ""
    skip_mult = float(h.get("skip_mark_cap_mult") or 1.3)
    boost_b = set(h.get("shared_boost_buckets") or [])
    boost_j = set(h.get("shared_boost_jobs") or [])
    boost = float(h.get("shared_boost") or 1.15)
    bucket_priors = cfg.get("priors") or {}
    for f in files:
        jobs = jobs_for_path(f["path"], cfg)
        if job not in jobs:
            continue
        b = bucket_of(f["path"], cfg)
        prior = priors.get(f["path"], bucket_priors.get(b, base))
        if skip_sub and skip_sub in f["path"]:
            prior = min(prior, base * skip_mult)
        path_lift = prior / base
        if b in boost_b and job in boost_j:
            path_lift *= boost
        lift = max(lift, path_lift)
    return round(min(job_base * lift, cap), 6)


def aggregate_attempt_labels(attempts: list[dict]) -> dict:
    """Combine one PR's per-build job results, oldest first.

    A job is `failed` if any attempt failed, else `succeeded` if any attempt succeeded, else
    the latest non-null result. The latest attempt alone hides failures fixed before merge.
    """
    out: dict = {}
    for labels in attempts:
        for job, res in labels.items():
            prev = out.get(job)
            if prev == "failed":
                continue
            if res == "failed":
                out[job] = "failed"
            elif res == "succeeded":
                out[job] = "succeeded"
            elif res is not None and prev != "succeeded":
                out[job] = res
            else:
                out.setdefault(job, res)
    return out


def _expand_job_list(spec, pretest: list[str], gold: list[str]) -> list[str]:
    out = []
    for item in spec:
        if item == "pretest":
            out.extend(pretest)
        elif item == "gold":
            out.extend(gold)
        else:
            out.append(item)
    return _uniq(out)


def _uniq(xs: list[str]) -> list[str]:
    seen = set()
    out = []
    for x in xs:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def classify_azure_job(name: str, cfg: dict):
    groups = (cfg.get("jobs") or {}).get("groups") or {}
    for group, mapping in groups.items():
        if name in mapping:
            return group, mapping[name]
    return None, None


def is_submodule_sha_only(files: list, cfg: dict) -> bool:
    if not files:
        return False
    rules = (cfg.get("paths") or {}).get("submodule_only") or {}
    prefixes = tuple(rules.get("prefixes", ["src/"]))
    extra = set(rules.get("extra_paths", [".gitmodules"]))
    for f in files:
        p = f.get("path") or ""
        if p in extra:
            continue
        ok = False
        for pref in prefixes:
            pref = pref.rstrip("/")
            if p == pref or p.startswith(pref + "/"):
                rest = p[len(pref) :].lstrip("/")
                if rest and "/" not in rest:
                    ok = True
                    break
        if not ok:
            return False
        add = f.get("additions")
        dele = f.get("deletions")
        if add not in (None, 0, 1) or dele not in (None, 0, 1):
            return False
    return True
