"""Azure DevOps REST collectors for calibration raw dumps."""

from __future__ import annotations

import collections
import logging
import os
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from ..repos.base import RepoAdapter
from ._http import get
from ._io import cache_exists, dump, now_iso, progress, try_load
from .calibration_rules import classify_azure_job, excluded_jobs, gold_jobs

logger = logging.getLogger(__name__)


def azure(org_url: str, path: str, params=None, ua=None):
    query = dict(params or {})
    query.setdefault("api-version", "7.1")
    url = f"{org_url}/{path}?{urllib.parse.urlencode(query, safe=',')}"
    payload, hdrs = get(url, ua=ua)
    token = hdrs.get("x-ms-continuationtoken") or hdrs.get("X-MS-ContinuationToken")
    return payload, token


def find_definition(adapter: RepoAdapter, out: str, ua: str | None, resume: bool = True) -> int:
    az = adapter.azure_devops
    if az is None:
        raise ValueError(f"Adapter {adapter.name!r} has no azure_devops spec")
    if resume and cache_exists(out, "azure-definitions.json"):
        cached = try_load(out, "azure-definitions.json") or {}
        pr_id = cached.get("pr_pipeline_definition_id")
        if pr_id is not None:
            logger.debug("Using cached azure-definitions.json (definition id %s)", pr_id)
            return int(pr_id)
    org = az.org_url
    definition, _ = azure(org, "build/definitions", {"name": az.pipeline_name, "$top": "50"}, ua)
    named = [{"id": item["id"], "name": item["name"]} for item in definition.get("value", [])]
    pr_id = az.definition_id or (named[0]["id"] if named else None)
    dump(out, "azure-definitions.json", {"named": named, "pr_pipeline_definition_id": pr_id})
    return int(pr_id)


def page_builds(org: str, ua: str | None, params: dict, max_items: int = 300, max_pages: int | None = None):
    items = []
    token = None
    page_size = max(1, int(params.get("$top") or 100))
    pages = max_pages if max_pages is not None else max_items // page_size + 1
    for _ in range(pages):
        query = dict(params)
        if token:
            query["continuationToken"] = token
        payload, token = azure(org, "build/builds", query, ua)
        items.extend(payload.get("value", []))
        if not token or not payload.get("value") or len(items) >= max_items:
            break
    return items[:max_items]


def _merge_build_lists(existing: list[dict], new_items: list[dict]) -> list[dict]:
    by_id = {int(item["id"]): item for item in existing if item.get("id") is not None}
    for item in new_items:
        if item.get("id") is not None:
            by_id[int(item["id"])] = item
    return sorted(by_id.values(), key=lambda row: row.get("finishTime") or "", reverse=True)


def select_attempts(builds: list[dict], max_prs: int, per_pr: int) -> list[dict]:
    """Newest `max_prs` PRs from a newest-first build list, with up to `per_pr` attempts each."""
    by_pr: dict[int, list[dict]] = {}
    for build in builds:
        pr = build.get("pr")
        if pr is None:
            continue
        if pr not in by_pr and len(by_pr) >= max_prs:
            continue
        attempts = by_pr.setdefault(pr, [])
        if len(attempts) < per_pr:
            attempts.append(build)
    return [build for attempts in by_pr.values() for build in attempts]


def _min_time_overlap(builds: list[dict]) -> str | None:
    times = [row.get("finishTime") for row in builds if row.get("finishTime")]
    if not times:
        return None
    newest = max(times)
    try:
        parsed = datetime.fromisoformat(newest.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed - timedelta(days=1)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def slim_build(build: dict) -> dict:
    src = build.get("sourceBranch") or ""
    pr = None
    if "/pull/" in src:
        try:
            pr = int(src.split("/pull/")[1].split("/")[0])
        except (ValueError, IndexError):
            pr = None
    return {
        "id": build.get("id"),
        "result": build.get("result"),
        "sourceBranch": src,
        "sourceVersion": build.get("sourceVersion"),
        "pr": pr,
        "finishTime": build.get("finishTime"),
        "startTime": build.get("startTime"),
    }


def parse_timeline(records: list, cfg: dict):
    group_jobs = collections.defaultdict(dict)
    jobs = []
    for record in records:
        typ = record.get("type")
        name = record.get("name") or ""
        result = record.get("result")
        if typ == "Job":
            jobs.append({"name": name, "result": result})
            group, key = classify_azure_job(name, cfg)
            if group and key and result:
                group_jobs[group][key] = result
    return {"groupJobs": dict(group_jobs), "jobs": jobs}


def default_azure_workers() -> int:
    raw = (os.environ.get("SCOUT_AZURE_WORKERS") or "6").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 6
    return max(1, min(value, 32))


def timeline_record_for_build(
    org: str,
    build: dict,
    cfg: dict,
    ua: str | None,
    legacy_fields: dict,
) -> dict | None:
    try:
        payload, _ = azure(org, f"build/builds/{build['id']}/timeline", {}, ua)
    except Exception as error:
        logger.warning("Azure timeline fetch failed for build %s: %s", build["id"], error)
        return None
    parsed = parse_timeline(payload.get("records") or [], cfg)
    record = {
        "id": build["id"],
        "pr": build["pr"],
        "result": build["result"],
        "pipelineResult": build["result"],
        "sourceVersion": build["sourceVersion"],
        "finishTime": build["finishTime"],
        "groupJobs": parsed["groupJobs"],
        "jobs": parsed["jobs"],
    }
    for group, field in legacy_fields.items():
        record[field] = parsed["groupJobs"].get(group) or {}
    if "image" in parsed["groupJobs"]:
        image = parsed["groupJobs"]["image"]
        failed = [job for job, result in image.items() if result == "failed"]
        record["imageJobs"] = image
        record["nFailedImageJobs"] = len(failed)
    return record


def fetch_azure_timelines(
    adapter: RepoAdapter,
    out: str,
    def_id: int,
    ua: str | None,
    collected: str,
    workers: int = 1,
    resume: bool = True,
) -> None:
    az = adapter.azure_devops
    assert az is not None
    cfg = join_cfg(adapter)
    org = az.org_url
    max_tl = int(az.pr_timelines)
    slim: list[dict]
    base_params = {
        "definitions": str(def_id),
        "reasonFilter": "pullRequest",
        "statusFilter": "completed",
        "$top": "100",
        "queryOrder": "finishTimeDescending",
    }
    if resume and cache_exists(out, "azure-pr-pipeline-builds.json"):
        cached_builds = try_load(out, "azure-pr-pipeline-builds.json") or {}
        slim = list(cached_builds.get("builds") or [])
        logger.info("Using cached azure-pr-pipeline-builds.json (%d builds)", len(slim))
        min_time = _min_time_overlap(slim)
        merged = slim
        if min_time:
            params = dict(base_params)
            params["minTime"] = min_time
            delta = page_builds(org, ua, params, max_items=int(az.pr_builds))
            merged = _merge_build_lists(merged, [slim_build(item) for item in delta])
        oldest = min((row.get("finishTime") for row in merged if row.get("finishTime")), default=None)
        missing = int(az.pr_builds) - len(merged)
        if oldest and missing > 0:
            params = dict(base_params)
            params["maxTime"] = oldest
            older = page_builds(org, ua, params, max_items=missing)
            merged = _merge_build_lists(merged, [slim_build(item) for item in older])
        if len(merged) != len(slim):
            logger.info("Azure builds: merged %d new, updated or older build(s)", len(merged) - len(slim))
            slim = merged
            dump(out, "azure-pr-pipeline-builds.json", {"n": len(slim), "builds": slim})
    else:
        builds = page_builds(org, ua, base_params, max_items=int(az.pr_builds))
        slim = [slim_build(item) for item in builds]
        dump(out, "azure-pr-pipeline-builds.json", {"n": len(slim), "builds": slim})
    sample = select_attempts(slim, max_tl, int(az.builds_per_pr))
    legacy_fields = (cfg.get("jobs") or {}).get("legacy_timeline_fields") or {}
    workers = max(1, int(workers))
    cached_by_id: dict[int, dict] = {}
    if resume:
        prev = try_load(out, "azure-pr-job-timelines.json")
        if prev:
            for record in prev.get("builds") or []:
                build_id = record.get("id")
                if build_id is not None:
                    cached_by_id[int(build_id)] = record
    pending = [build for build in sample if int(build["id"]) not in cached_by_id]
    logger.info(
        "Azure timelines: %d cached, %d to fetch (%d worker(s))",
        len(sample) - len(pending),
        len(pending),
        workers,
    )
    if not pending and len(cached_by_id) >= len(sample):
        logger.info("All %d timeline(s) already cached; skipping HTTP", len(sample))
    timelines_by_id = {
        int(build["id"]): cached_by_id[int(build["id"])]
        for build in sample
        if int(build["id"]) in cached_by_id
    }
    if workers == 1:
        for index, build in enumerate(pending, 1):
            record = timeline_record_for_build(org, build, cfg, ua, legacy_fields)
            if record is not None:
                timelines_by_id[int(build["id"])] = record
            progress(index, len(pending), "Azure timeline", every=25, unit="PRs")
            time.sleep(0.04)
    elif pending:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(timeline_record_for_build, org, build, cfg, ua, legacy_fields): build
                for build in pending
            }
            for index, future in enumerate(as_completed(futures), 1):
                build = futures[future]
                record = future.result()
                if record is not None:
                    timelines_by_id[int(build["id"])] = record
                progress(index, len(pending), "Azure timeline", every=25, unit="PRs")
    timelines = [
        timelines_by_id[int(build["id"])]
        for build in sample
        if int(build["id"]) in timelines_by_id
    ]
    timelines.sort(key=lambda row: row.get("finishTime") or "", reverse=True)
    gold = gold_jobs(cfg)
    excl = excluded_jobs(cfg)
    dump(
        out,
        "azure-pr-job-timelines.json",
        {
            "n": len(timelines),
            "gold_jobs": gold,
            "excluded_jobs": excl,
            "builds": timelines,
            "collected_at": collected,
        },
    )


def join_cfg(adapter: RepoAdapter) -> dict:
    if adapter.calibration is None:
        raise ValueError(f"Adapter {adapter.name!r} has no calibration tables")
    return dict(adapter.calibration.tables)


def run_azure(
    adapter: RepoAdapter,
    out_dir: str,
    collected: str | None = None,
    workers: int | None = None,
    resume: bool = True,
) -> None:
    collected = collected or now_iso()
    ua = adapter.github_api.user_agent if adapter.github_api else None
    logger.info("Resolving Azure pipeline definition")
    def_id = find_definition(adapter, out_dir, ua, resume=resume)
    parallel = workers if workers is not None else default_azure_workers()
    fetch_azure_timelines(adapter, out_dir, def_id, ua, collected, workers=parallel, resume=resume)
