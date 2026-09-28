"""GitHub REST transport for live PR listing (step E watch)."""

from __future__ import annotations

import logging
import urllib.parse

from ._http import get

logger = logging.getLogger(__name__)


def gh(url: str, ua: str | None):
    payload, hdrs = get(url, extra_headers={"Accept": "application/vnd.github+json"}, ua=ua)
    remaining = hdrs.get("X-RateLimit-Remaining") or hdrs.get("x-ratelimit-remaining")
    limit = hdrs.get("X-RateLimit-Limit") or hdrs.get("x-ratelimit-limit")
    logger.debug(
        "GitHub rate limit %s/%s %s",
        remaining,
        limit,
        url.split("?")[0][-60:],
    )
    return payload


def list_open_prs(owner: str, repo: str, *, user_agent: str | None = None) -> list[dict]:
    """Return open pull requests (number, head sha, title) for ``owner/repo``."""
    base = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    page = 1
    rows: list[dict] = []
    while True:
        query = urllib.parse.urlencode({"state": "open", "per_page": "100", "page": str(page)})
        batch = gh(f"{base}?{query}", user_agent)
        if not isinstance(batch, list) or not batch:
            break
        for item in batch:
            head = item.get("head") or {}
            rows.append(
                {
                    "number": item.get("number"),
                    "head_sha": head.get("sha"),
                    "title": item.get("title"),
                }
            )
        if len(batch) < 100:
            break
        page += 1
    return rows


def list_pr_files(owner: str, repo: str, number: int, *, user_agent: str | None = None) -> list[str]:
    """Paths a pull request changes, as GitHub's three-dot diff shows them (GitHub stops at 3000)."""
    base = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}/files"
    page = 1
    paths: list[str] = []
    while True:
        query = urllib.parse.urlencode({"per_page": "100", "page": str(page)})
        batch = gh(f"{base}?{query}", user_agent)
        if not isinstance(batch, list) or not batch:
            break
        paths.extend(item["filename"] for item in batch if item.get("filename"))
        if len(batch) < 100:
            break
        page += 1
    return paths
