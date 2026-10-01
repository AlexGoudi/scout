"""One review end to end: brief, then the agent stage, then the report, and all four artifacts.

The orchestrator's contract is that a brief and a report are always
produced, including when no model was reachable, so the order of writes is part of the
design. The brief is written the moment stage 1 finishes, before any model is asked
anything, so a run that dies in stage 2 still leaves the artifact that needed no model; the
run log streams as it goes, so the trace of a killed run survives it; the report and the
comment follow. Stage 2 and the report builder never raise for anything a model or a budget
can do, so short of a bug the only failures that stop a review are stage 1's own, which
leave no brief to report on.

| Artifact | Written by | When |
| --- | --- | --- |
| `scout-brief.json` | stage 1 | as soon as it validates |
| `scout-run.jsonl` | every stage | record by record, as the run goes |
| `scout-report.json` | stage 4 | after stage 2, validated on write |
| `scout-comment.md` | stage 4 | rendered from the report and nothing else |

For a backtest harness, which drives stages without the CLI:

    run_agent_stage(brief, source, change_set=None, provider=None, run_log=None,
                    deadline_s=DEFAULT_DEADLINE_S) -> AgentResult
    build_report(brief, agent, brief_ref="scout-brief.json", run_id=None, duration_s=None) -> Report

and `run_review` / `review_fixture` for the whole pipeline over a source or a pinned fixture.
"""

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Union

from ..agent.budget import DEFAULT_DEADLINE_S
from ..agent.runner import AgentResult, run_agent
from ..models import ChangeSet, MODE_RANGE
from ..provider import Provider
from ..repos import RepoAdapter, get_adapter
from ..report.builder import Report, build_report
from ..report.render import render_comment
from ..source import RepoSource
from ..static.brief import Brief
from ..static.engine import analyze, build_brief
from .review_fixture import ReviewFixture
from .runlog import RunLog

logger = logging.getLogger(__name__)

BRIEF_FILE = "scout-brief.json"
REPORT_FILE = "scout-report.json"
COMMENT_FILE = "scout-comment.md"
RUN_LOG_FILE = "scout-run.jsonl"


@dataclass(frozen=True)
class ReviewResult:
    """Everything one review produced, and where it was written."""

    brief: Brief
    agent: AgentResult
    report: Report
    comment: str
    change_set: Optional[ChangeSet]
    duration_s: float
    paths: Dict[str, Path] = field(default_factory=dict)


def run_agent_stage(
    brief: Union[Brief, Dict[str, Any]],
    source: RepoSource,
    change_set: Optional[ChangeSet] = None,
    provider: Optional[Provider] = None,
    run_log: Optional[RunLog] = None,
    deadline_s: float = DEFAULT_DEADLINE_S,
) -> AgentResult:
    """Stage 2 alone, over a brief built elsewhere. The brief is validated on read."""
    return run_agent(brief, source, change_set=change_set, provider=provider, run_log=run_log,
                     deadline_s=deadline_s)


def run_review(
    source: RepoSource,
    repo: str,
    adapter: RepoAdapter,
    rev: str,
    change_set: Optional[ChangeSet] = None,
    provider: Optional[Provider] = None,
    output_dir: Optional[Path] = None,
    mode: str = MODE_RANGE,
    hotspot_limit: int = 10,
    run_id: Optional[str] = None,
    measured_at: Optional[str] = None,
    deadline_s: float = DEFAULT_DEADLINE_S,
) -> ReviewResult:
    """Brief, agent, report over one change set (or a whole tree), writing to `output_dir` when given."""
    started = time.monotonic()
    output = Path(output_dir) if output_dir is not None else None
    if output is not None:
        output.mkdir(parents=True, exist_ok=True)
    log = RunLog(output / RUN_LOG_FILE if output is not None else None)
    paths: Dict[str, Path] = {}

    log.emit("run_start", repo=repo, adapter=adapter.name, rev=rev, mode=mode,
             base=change_set.base_sha if change_set else "", commits=change_set.commit_count if change_set else 0,
             provider=provider.spec.provider if provider else "none",
             model=provider.spec.model_id if provider else "")
    log.emit("stage_start", stage="static")
    result = analyze(source, rev, adapter, change_set=change_set, hotspot_limit=hotspot_limit)
    brief = build_brief(result, repo=repo, base_sha=change_set.base_sha if change_set else "", head_sha=rev,
                        mode=mode if change_set is not None else "tree", run_id=run_id, measured_at=measured_at)
    coverage = brief.coverage
    log.emit("stage_end", stage="static", duration_s=round(result.duration_s, 3), blobs_read=result.blobs_read,
             affected=len(coverage["affected"]), covered=len(coverage["covered"]),
             uncovered=len(coverage["uncovered"]), ambiguous=len(coverage["ambiguous"]),
             questions=[question["id"] for question in brief.payload["questions"]])
    if output is not None:
        paths["brief"] = output / BRIEF_FILE
        brief.write(paths["brief"])
        logger.info("Wrote %s (%s)", paths["brief"], brief.sha()[:12])

    agent = run_agent(brief, source, change_set=change_set, provider=provider, run_log=log, deadline_s=deadline_s)
    log.emit("stage_start", stage="report")
    report = build_report(brief, agent, brief_ref=BRIEF_FILE, run_id=run_id or brief.id,
                          duration_s=time.monotonic() - started)
    comment = render_comment(report, brief)
    log.emit("stage_end", stage="report", findings=len(report.findings),
             posted=sum(1 for finding in report.findings if finding["adjudication"]["posted"]),
             suppressed=len(report.payload["suppressed"]))
    if output is not None:
        paths["report"] = output / REPORT_FILE
        paths["comment"] = output / COMMENT_FILE
        paths["run_log"] = output / RUN_LOG_FILE
        report.write(paths["report"])
        paths["comment"].write_text(comment, encoding="utf-8")

    duration = time.monotonic() - started
    log.emit("run_end", status=report.status, duration_s=round(duration, 3), findings=len(report.findings),
             model_calls=agent.model_calls, input_tokens=agent.usage.input_tokens,
             output_tokens=agent.usage.output_tokens, checks=agent.checks)
    return ReviewResult(brief=brief, agent=agent, report=report, comment=comment, change_set=change_set,
                        duration_s=duration, paths=paths)


def review_fixture(
    fixture_path: Path,
    provider: Optional[Provider] = None,
    output_dir: Optional[Path] = None,
    hotspot_limit: int = 10,
    deadline_s: float = DEFAULT_DEADLINE_S,
) -> ReviewResult:
    """A review replayed from a pinned fixture: no git, no network, and with a replay provider, no model."""
    fixture = ReviewFixture.load(Path(fixture_path))
    return run_review(
        fixture.source(),
        repo=fixture.repo,
        adapter=get_adapter(fixture.adapter),
        rev=fixture.head.rev,
        change_set=fixture.change_set,
        provider=provider,
        output_dir=output_dir,
        mode=fixture.mode,
        hotspot_limit=hotspot_limit,
        run_id=fixture.run_id or None,
        measured_at=fixture.measured_at or None,
        deadline_s=deadline_s,
    )
