"""Incident miner: turns the repo's revert history into the labelled seed corpus.

Git history is already a labelled dataset (HLD section 6.3). This module finds the
`^Revert` commits, links the ones carrying a `This reverts commit <sha>` trailer back to
the commit they revert, and emits one JSONL record per incident. `detector_category` is
deliberately left unset: R2 adjudicates it in the joint triage session on D2, per
docs/scout-plan.md section 8.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .gitcmd import GitRepo
from .mining.message import PR_SUFFIX_RE, REVERTS_SHA_RE

logger = logging.getLogger(__name__)

CORPUS_VERSION = "1.0"
DEFAULT_GREP = "^Revert"

LINK_LINKED = "linked"
LINK_UNLINKED = "unlinked"
LINK_UNRESOLVED = "unresolved"

_RECORD_SEPARATOR = "\x1e"
_FIELD_SEPARATOR = "\x1f"
_LOG_FORMAT = _FIELD_SEPARATOR.join(["%H", "%aI", "%cI", "%s", "%B"]) + _FIELD_SEPARATOR

_NESTED_REVERT_RE = re.compile(r'^Revert\s+"Revert')


@dataclass(frozen=True)
class Incident:
    """One corpus record: a revert and, where linkable, the commit it reverted."""

    incident_id: str
    revert_sha: str
    revert_subject: str
    revert_authored_date: str
    revert_committed_date: str
    revert_paths: List[str] = field(default_factory=list)
    link_status: str = LINK_UNLINKED
    reverted_sha: Optional[str] = None
    reverted_subject: Optional[str] = None
    reverted_authored_date: Optional[str] = None
    reverted_committed_date: Optional[str] = None
    reverted_paths: List[str] = field(default_factory=list)
    lead_time_days: Optional[int] = None
    pr_number: Optional[int] = None
    is_nested_revert: bool = False
    additional_reverted_shas: List[str] = field(default_factory=list)
    detector_category: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "corpus_version": CORPUS_VERSION,
            "incident_id": self.incident_id,
            "revert_sha": self.revert_sha,
            "revert_subject": self.revert_subject,
            "revert_authored_date": self.revert_authored_date,
            "revert_committed_date": self.revert_committed_date,
            "revert_paths": list(self.revert_paths),
            "link_status": self.link_status,
            "reverted_sha": self.reverted_sha,
            "reverted_subject": self.reverted_subject,
            "reverted_authored_date": self.reverted_authored_date,
            "reverted_committed_date": self.reverted_committed_date,
            "reverted_paths": list(self.reverted_paths),
            "lead_time_days": self.lead_time_days,
            "pr_number": self.pr_number,
            "is_nested_revert": self.is_nested_revert,
            "additional_reverted_shas": list(self.additional_reverted_shas),
            "detector_category": self.detector_category,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "Incident":
        return cls(
            incident_id=str(payload["incident_id"]),
            revert_sha=str(payload["revert_sha"]),
            revert_subject=str(payload.get("revert_subject") or ""),
            revert_authored_date=str(payload.get("revert_authored_date") or ""),
            revert_committed_date=str(payload.get("revert_committed_date") or ""),
            revert_paths=[str(item) for item in payload.get("revert_paths") or []],
            link_status=str(payload.get("link_status") or LINK_UNLINKED),
            reverted_sha=payload.get("reverted_sha"),
            reverted_subject=payload.get("reverted_subject"),
            reverted_authored_date=payload.get("reverted_authored_date"),
            reverted_committed_date=payload.get("reverted_committed_date"),
            reverted_paths=[str(item) for item in payload.get("reverted_paths") or []],
            lead_time_days=payload.get("lead_time_days"),
            pr_number=payload.get("pr_number"),
            is_nested_revert=bool(payload.get("is_nested_revert")),
            additional_reverted_shas=[str(item) for item in payload.get("additional_reverted_shas") or []],
            detector_category=payload.get("detector_category"),
        )


@dataclass(frozen=True)
class _RawCommit:
    sha: str
    authored_date: str
    committed_date: str
    subject: str
    body: str
    paths: List[str]


def mine_incidents(
    repo: GitRepo,
    revision: str = "HEAD",
    limit: Optional[int] = None,
    grep: str = DEFAULT_GREP,
    range_spec: Optional[str] = None,
) -> List[Incident]:
    """Mine revert incidents from `revision`, newest first.

    When ``range_spec`` is set (e.g. ``abc..def``), only commits in that range are walked.
    """
    log_args = ["log", f"--grep={grep}"]
    if limit:
        log_args += ["-n", str(limit)]
    if range_spec:
        log_args.append(range_spec)
    else:
        log_args.append(revision)

    reverts = _read_commits(repo, log_args)
    logger.info("Found %d commit(s) matching %s on %s", len(reverts), grep, revision)

    referenced = sorted({sha for revert in reverts for sha in REVERTS_SHA_RE.findall(revert.body)})
    resolved = repo.resolve_commits(referenced)
    logger.info("Resolved %d of %d referenced sha(s) in this clone", len(resolved), len(referenced))

    causes = _load_causes(repo, sorted(set(resolved.values())))

    incidents: List[Incident] = []
    for index, revert in enumerate(reverts, start=1):
        incidents.append(_build_incident(f"incident-{index:04d}", revert, resolved, causes))
    return incidents


def _build_incident(
    incident_id: str,
    revert: _RawCommit,
    resolved: Dict[str, str],
    causes: Dict[str, _RawCommit],
) -> Incident:
    referenced = REVERTS_SHA_RE.findall(revert.body)
    pr_match = PR_SUFFIX_RE.search(revert.subject)
    common = {
        "incident_id": incident_id,
        "revert_sha": revert.sha,
        "revert_subject": revert.subject,
        "revert_authored_date": revert.authored_date,
        "revert_committed_date": revert.committed_date,
        "revert_paths": revert.paths,
        "pr_number": int(pr_match.group(1)) if pr_match else None,
        "is_nested_revert": bool(_NESTED_REVERT_RE.match(revert.subject)),
        "additional_reverted_shas": [resolved.get(sha, sha) for sha in referenced[1:]],
    }

    if not referenced:
        return Incident(link_status=LINK_UNLINKED, **common)

    cause_sha = resolved.get(referenced[0])
    cause = causes.get(cause_sha) if cause_sha else None
    if cause is None:
        # The trailer names a commit that is not in this clone, e.g. reverted on a branch
        # whose history was never merged here. Keep the record, drop the derived fields.
        return Incident(link_status=LINK_UNRESOLVED, reverted_sha=referenced[0], **common)

    return Incident(
        link_status=LINK_LINKED,
        reverted_sha=cause.sha,
        reverted_subject=cause.subject,
        reverted_authored_date=cause.authored_date,
        reverted_committed_date=cause.committed_date,
        reverted_paths=cause.paths,
        lead_time_days=_days_between(cause.committed_date, revert.committed_date),
        **common,
    )


def _load_causes(repo: GitRepo, shas: List[str]) -> Dict[str, _RawCommit]:
    if not shas:
        return {}
    commits = _read_commits(repo, ["log", "--no-walk"] + shas)
    return {commit.sha: commit for commit in commits}


def _read_commits(repo: GitRepo, log_args: List[str]) -> List[_RawCommit]:
    output = repo.run(*log_args, "--name-only", f"--format={_RECORD_SEPARATOR}{_LOG_FORMAT}")

    commits: List[_RawCommit] = []
    for record in output.split(_RECORD_SEPARATOR):
        if not record.strip():
            continue
        sha, authored_date, committed_date, subject, body, paths_blob = record.split(_FIELD_SEPARATOR, 5)
        commits.append(
            _RawCommit(
                sha=sha,
                authored_date=authored_date,
                committed_date=committed_date,
                subject=subject,
                body=body,
                paths=[line for line in paths_blob.splitlines() if line.strip()],
            )
        )
    return commits


def _days_between(earlier: str, later: str) -> Optional[int]:
    """Whole days between two git ISO-8601 dates, or None if either fails to parse."""
    try:
        start = datetime.fromisoformat(earlier)
        end = datetime.fromisoformat(later)
    except ValueError:
        logger.warning("Unable to parse commit dates %r and %r", earlier, later)
        return None
    return int((end - start).total_seconds() // 86400)


def write_corpus(incidents: List[Incident], path: Path) -> None:
    """Write the corpus as JSONL, one incident per line."""
    if path.parent not in (Path(), Path(".")):
        path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for incident in incidents:
            handle.write(json.dumps(incident.to_dict(), sort_keys=True) + "\n")
    logger.info("Wrote %d incident(s) to %s", len(incidents), path)


def read_corpus(path: Path) -> List[Incident]:
    with path.open("r", encoding="utf-8") as handle:
        return [Incident.from_dict(json.loads(line)) for line in handle if line.strip()]


def summarize(incidents: List[Incident]) -> Dict[str, Any]:
    """Counts the plan tracks: how many reverts, how many auto-linkable, lead times."""
    lead_times = sorted(
        incident.lead_time_days for incident in incidents if incident.lead_time_days is not None
    )
    summary: Dict[str, Any] = {
        "reverts": len(incidents),
        "linked": sum(1 for incident in incidents if incident.link_status == LINK_LINKED),
        "unlinked": sum(1 for incident in incidents if incident.link_status == LINK_UNLINKED),
        "unresolved": sum(1 for incident in incidents if incident.link_status == LINK_UNRESOLVED),
        "nested_reverts": sum(1 for incident in incidents if incident.is_nested_revert),
        "with_pr_number": sum(1 for incident in incidents if incident.pr_number is not None),
        "categorized": sum(1 for incident in incidents if incident.detector_category),
        "lead_time_days": {
            "count": len(lead_times),
            "negative": sum(1 for value in lead_times if value < 0),
            "min": lead_times[0] if lead_times else None,
            "median": lead_times[len(lead_times) // 2] if lead_times else None,
            "max": lead_times[-1] if lead_times else None,
        },
    }
    return summary
