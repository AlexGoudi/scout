"""HTTP GET with retries and optional GitHub token (eval collectors)."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

# Client errors a retry cannot fix; 403 and 429 stay retryable because GitHub rate limits use them.
RETRYLESS_STATUS = frozenset({400, 401, 404, 410, 422})


def github_token() -> str:
    return (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()


def _rate_limit_wait_seconds(headers) -> int | None:
    reset = headers.get("X-RateLimit-Reset") or headers.get("x-ratelimit-reset")
    if not reset:
        return None
    try:
        wait = int(reset) - int(time.time()) + 1
    except (TypeError, ValueError):
        return None
    if wait <= 0 or wait > 7200:
        return None
    return wait


def _github_rate_limit_message(error: urllib.error.HTTPError) -> str:
    if "api.github.com" not in (error.url or ""):
        return str(error)
    remaining = error.headers.get("X-RateLimit-Remaining") or error.headers.get("x-ratelimit-remaining")
    limit = error.headers.get("X-RateLimit-Limit") or error.headers.get("x-ratelimit-limit")
    reset = error.headers.get("X-RateLimit-Reset") or error.headers.get("x-ratelimit-reset")
    parts = [f"GitHub HTTP {error.code}: rate limit exceeded"]
    if limit is not None and remaining is not None:
        parts.append(f"(remaining {remaining}/{limit})")
    if reset:
        parts.append(f"resets at unix {reset}")
    if not github_token():
        parts.append(
            "No GITHUB_TOKEN/GH_TOKEN in the environment — anonymous REST is ~60 requests/hour per IP "
            "(search endpoints are tighter). Export a read-only PAT and retry."
        )
    else:
        parts.append("Token is set but quota is exhausted; wait for reset or use a different token.")
    return " ".join(parts)


def get(url, extra_headers=None, timeout=90, ua=None):
    hdrs = {"User-Agent": ua or "sonic-scout"}
    tok = github_token()
    if tok and "api.github.com" in url:
        hdrs["Authorization"] = f"Bearer {tok}"
    if extra_headers:
        hdrs.update(extra_headers)
    req = urllib.request.Request(url, headers=hdrs)
    last = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    data = {"_raw": body.decode("utf-8", "replace")[:2000]}
                return data, dict(r.headers)
        except urllib.error.HTTPError as error:
            last = error
            if error.code == 401 and "api.github.com" in url:
                raise urllib.error.HTTPError(
                    error.url,
                    error.code,
                    "GitHub HTTP 401: bad credentials. GITHUB_TOKEN/GH_TOKEN is expired, revoked or mistyped; "
                    "replace it (a read-only fine-grained token is enough) or unset it to go anonymous",
                    error.headers,
                    error.fp,
                ) from error
            if error.code in RETRYLESS_STATUS:
                raise
            if error.code in (403, 429) and "api.github.com" in url:
                wait = _rate_limit_wait_seconds(error.headers)
                if wait is not None and attempt < 4:
                    time.sleep(min(wait, 300))
                    continue
            time.sleep(1.5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError) as error:
            last = error
            time.sleep(1.5 * (attempt + 1))
    if isinstance(last, urllib.error.HTTPError) and last.code in (403, 429):
        raise urllib.error.HTTPError(
            last.url,
            last.code,
            _github_rate_limit_message(last),
            last.headers,
            last.fp,
        ) from last
    raise last
