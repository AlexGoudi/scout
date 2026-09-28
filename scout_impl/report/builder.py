"""Build `scout-report.json` from a brief and the agent stage's result (HLD section 4.10).

One finding per brief question, because a finding's adjudication answers exactly one
question and question binding is a rule at this boundary too. Each finding carries HLD
4.10's blocks: a `deterministic` half stated from the brief's own numbers, `proven` by
construction; an `adjudication` half holding the model's answers group by group, with
what each check made of them and a band of its own; the evidence both rest on, every item
with the quote the resolver re-read; and the affected platforms, which are the brief's and
never the model's.

The report is validated on write, against its schema and then against what a schema cannot
express: that nothing names an entity outside the brief, that every finding answers one of
the brief's questions, that a contested adjudication is banded no higher than `medium`,
that a finding ranks on its adjudication only when that adjudication is usable, and that
the coverage numbers are the brief's. A report failing any of these is a bug in Scout, and
is refused rather than written.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from ..agent.assemble import BriefView, Group
from ..agent.checks import OUTCOME_CONTESTED, OUTCOME_DROPPED, OUTCOME_UNEXAMINED
from ..agent.evidence import ROLE_AFFECTED, ROLE_CAUSE, ROLE_CONTRACT, Evidence
from ..agent.protocol import VERDICT_MATERIAL, VERDICT_PASS_THROUGH, VERDICT_UNCLEAR
from ..agent.runner import Q_ANSWERED, Q_NOT_RUN, Q_TRUNCATED, AgentResult, QuestionResult
from ..detectors.base import QUESTION_AMBIGUITY
from ..static.brief import Brief
from .schema import REPORT_SCHEMA_VERSION, validate_report
from .scoring import (
    BAND_HIGH,
    BAND_MEDIUM,
    BAND_PROVEN,
    BAND_RANK,
    SEVERITY_RANK,
    Confidence,
    band_adjudication,
    posts,
    severity_for,
)

DEFAULT_BRIEF_REF = "scout-brief.json"
DETECTOR = "D6"
MAX_FINDINGS = 10
MAX_EVIDENCE = 8
MEASUREMENTS = ("id", "duration_s", "static_duration_s", "agent_duration_s")
VERIFICATION = {"method": "none", "result": "not-applicable",
                "reason": "a deterministic claim about two files in the tree; re-running the lookup proves nothing"}


class ReportContractError(ValueError):
    """The report satisfies its schema but breaks a contract the schema cannot express."""

    def __init__(self, problems: List[str]) -> None:
        self.problems = tuple(problems)
        listed = "\n  ".join(problems)
        super().__init__(f"{len(problems)} report contract violation(s):\n  {listed}")


@dataclass(frozen=True)
class Report:
    """A validated report, and the bytes it serializes to."""

    payload: Dict[str, Any]

    @property
    def findings(self) -> List[Dict[str, Any]]:
        return list(self.payload["findings"])

    @property
    def status(self) -> str:
        return str(self.payload["run"]["status"])

    def to_json(self) -> str:
        return json.dumps(self.payload, indent=2, sort_keys=True) + "\n"

    def canonical_json(self) -> str:
        """The report without what a rerun cannot reproduce: its id, its timings, whether it was replayed."""
        payload = json.loads(json.dumps(self.payload))
        run = payload["run"]
        for name in MEASUREMENTS:
            run.pop(name, None)
        run.get("model", {}).pop("replayed", None)
        for question in run.get("questions", []):
            question.pop("latency_s", None)
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"

    def sha(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def write(self, path: Path) -> None:
        Path(path).write_text(self.to_json(), encoding="utf-8")

    def validate(self, brief: Optional[Dict[str, Any]] = None) -> None:
        validate_report(self.payload)
        if brief is not None:
            _check_contracts(self.payload, brief)


def build_report(
    brief: Union[Brief, Dict[str, Any]],
    agent: AgentResult,
    brief_ref: str = DEFAULT_BRIEF_REF,
    run_id: Optional[str] = None,
    duration_s: Optional[float] = None,
) -> Report:
    """Assemble, rank and validate the report for one review."""
    payload = brief.payload if isinstance(brief, Brief) else brief
    view = BriefView(payload)

    findings = []
    for question in view.questions:
        result = agent.question(question["id"]) or QuestionResult(
            question=question, kind=view.question_kind(question), status=Q_NOT_RUN,
            reason=agent.degraded_reason or "the agent stage did not reach this question")
        if result.kind == QUESTION_AMBIGUITY:
            findings.append(_ambiguity_finding(view, result, agent))
        else:
            findings.append(_materiality_finding(view, result, agent))

    findings.sort(key=_rank)
    suppressed = []
    higher = 0
    for index, finding in enumerate(findings, start=1):
        finding["id"] = f"f-{index:03d}"
        adjudication = finding["adjudication"]
        adjudication["posted"] = index <= MAX_FINDINGS and posts(adjudication["band"], higher)
        if adjudication["posted"]:
            higher += 1
        elif adjudication["band"] is not None:
            suppressed.append({"finding": finding["id"], "part": "adjudication", "band": adjudication["band"],
                               "reason": adjudication.get("band_reason") or "below the posting threshold"})
        if index > MAX_FINDINGS:
            suppressed.append({"finding": finding["id"], "part": "finding", "band": finding["confidence"]["band"],
                               "reason": f"beyond the NFR-4 cap of {MAX_FINDINGS} findings"})

    report = Report(payload={
        "schema_version": REPORT_SCHEMA_VERSION,
        "run": _run_block(view, Brief(payload=payload), agent, brief_ref, run_id, duration_s),
        "findings": findings,
        "suppressed": suppressed,
        "degraded_reason": agent.degraded_reason,
    })
    report.validate(payload)
    return report


def _run_block(view: BriefView, brief: Brief, agent: AgentResult, brief_ref: str, run_id: Optional[str],
               duration_s: Optional[float]) -> Dict[str, Any]:
    run = view.run
    static_s = float(run["static_duration_s"])
    return {
        "id": run_id or str(uuid.uuid4()),
        "repo": run["repo"],
        "adapter": run["adapter"]["name"],
        "mode": run["mode"],
        "base_sha": run["base_sha"],
        "head_sha": run["head_sha"],
        "brief_ref": brief_ref,
        "brief_sha": brief.sha(),
        "duration_s": round(duration_s if duration_s is not None else static_s + agent.duration_s, 3),
        "static_duration_s": round(static_s, 3),
        "agent_duration_s": round(agent.duration_s, 3),
        "status": agent.status,
        "model": {"provider": agent.provider, "id": agent.model_id, "prompt_sha": agent.prompt_sha,
                  "replayed": agent.replayed},
        "cost": {"input_tokens": agent.usage.input_tokens, "output_tokens": agent.usage.output_tokens,
                 "usd": round(agent.usd, 6)},
        "questions_in_brief": len(view.questions),
        "questions_asked": len(agent.asked),
        "questions_answered": len(agent.answered),
        "model_calls": agent.model_calls,
        "blobs_read": int(run["blobs_read"]) + agent.blobs_read,
        "truncated": agent.truncated,
        "checks": agent.checks,
        "questions": [
            {"id": result.id, "kind": result.kind, "status": result.status, "reason": result.reason,
             "model_calls": len(result.calls), "tool_steps": result.tool_steps,
             "input_tokens": result.usage.input_tokens, "output_tokens": result.usage.output_tokens,
             "latency_s": round(result.latency_s, 3)}
            for result in agent.questions
        ],
    }


def _materiality_finding(view: BriefView, result: QuestionResult, agent: AgentResult) -> Dict[str, Any]:
    question = result.question
    coverage = view.coverage
    platforms = [entity for entity in question["entities"] if entity.startswith("platform:")]
    families = _families(result, platforms)
    never_built = _never_built(view)
    unbuilt = sorted({family for names in families.values() for family in names})

    kept = [item for item in result.items if item.kept]
    confidence = _band(question, result, kept, undecided=_verdicts(kept) == {VERDICT_UNCLEAR})
    resolution = _materiality_resolution(kept, confidence)
    usable = confidence.band in (BAND_HIGH, BAND_MEDIUM)
    benign = usable and resolution == VERDICT_PASS_THROUGH
    count = len(platforms)
    if usable and resolution == VERDICT_MATERIAL:
        title = f"Change materially affects {count} platform(s) PR CI never builds"
    elif benign:
        title = f"Change passes through {count} platform(s) PR CI never builds without changing what they do"
    else:
        title = f"Change reaches {count} platform(s) PR CI never builds"

    named = f" ({', '.join(unbuilt)})" if unbuilt else ""
    statement = (
        f"Of {len(coverage['affected'])} platform(s) this change reaches, {len(coverage['covered'])} declare an "
        f"ASIC family one of the {coverage['job_groups']} PR-CI job groups builds and {count} declare only "
        f"families none of them builds{named}."
    )
    if coverage["ambiguous"]:
        statement += (f" {len(coverage['ambiguous'])} more declare a family built only for other architectures; "
                      f"with them, {never_built} are never built.")

    return {
        "id": "",
        "detector": DETECTOR,
        "question": question["id"],
        "title": title,
        "severity": severity_for(count, benign),
        "confidence": _confidence(confidence),
        "deterministic": {
            "band": BAND_PROVEN,
            "statement": statement,
            "affected_count": len(coverage["affected"]),
            "covered_by_pr_ci": len(coverage["covered"]),
            "uncovered_count": count,
            "ambiguous_count": len(coverage["ambiguous"]),
            "never_built": never_built,
        },
        "adjudication": _adjudication(result, kept, confidence, resolution, agent,
                                      _rationale(result, kept, lambda item: item.answer.get("verdict", ""))),
        "evidence": _evidence(result, kept),
        "affected": [
            {"kind": "platform", "id": entity, "asic_families": list(families.get(entity, ())),
             "arch": _arch(view, entity), "resolution": "uncovered",
             "reason": (f"declares only {', '.join(families[entity])}, which no PR-CI job group builds"
                        if families.get(entity) else "declares no family any PR-CI job group builds")}
            for entity in platforms
        ],
        "coverage_gap": _coverage_gap(view),
        "verification": dict(VERIFICATION),
        "backtest_ref": None,
    }


def _ambiguity_finding(view: BriefView, result: QuestionResult, agent: AgentResult) -> Dict[str, Any]:
    question = result.question
    candidate = view.rule_candidate(question) or {}
    platforms = list(candidate.get("platforms") or [])
    by_resolution: Dict[str, int] = {}
    for platform in platforms:
        by_resolution[platform["resolution"]] = by_resolution.get(platform["resolution"], 0) + 1
    families = sorted({family for platform in platforms for family in platform["families"]})
    qualified = [group for group in view.job_groups if group["family"] in families]
    built_for = "; ".join(f"{group['name']} builds {group['family']} only for {group['arch']}" for group in qualified)
    count = len(platforms)
    unbuilt = by_resolution.get("uncovered", 0)

    kept = [item for item in result.items if item.kept]
    contested = any(item.outcome == OUTCOME_CONTESTED for item in kept)
    confidence = _band(question, result, kept, contested=contested)

    rules = list(candidate.get("rules") or [question["rule"]])
    named = ", ".join(families) or "these families"
    statement = (
        f"No PR-CI job group builds {named} by name{': ' + built_for if built_for else ''}. "
        f"Of the {count} platform(s) this change reaches that declare {'it' if len(families) == 1 else 'them'}, "
        f"{' and '.join(rules)}, which rest on a naming convention rather than a declaration, find "
        f"{by_resolution.get('covered', 0)} built for their own CPU architecture and {unbuilt} not."
    )
    return {
        "id": "",
        "detector": DETECTOR,
        "question": question["id"],
        "title": (f"{unbuilt} of {count} platform(s) declaring {', '.join(families)} are never built for their "
                  f"CPU architecture" if candidate else
                  f"{count} platform(s) declare a family PR CI builds only for other architectures"),
        "severity": severity_for(unbuilt, False),
        "confidence": _confidence(confidence),
        "deterministic": {
            "band": BAND_PROVEN,
            "statement": statement,
            "affected_count": count,
            "covered_by_pr_ci": 0,
            "ambiguous_count": len(view.coverage["ambiguous"]),
            "never_built": unbuilt,
            "rule_candidate": {
                "rules": rules,
                "derivation": str(candidate.get("derivation") or "naming-convention-inference"),
                "statement": str(candidate.get("statement") or "no rule candidate in the brief"),
                "covered": by_resolution.get("covered", 0),
                "uncovered": unbuilt,
                "undetermined": by_resolution.get("undetermined", 0),
            },
        },
        "adjudication": _adjudication(result, kept, confidence, "architecture-aware" if kept else None, agent,
                                      _rationale(result, kept, _covered_phrase)),
        "evidence": _evidence(result, kept),
        "affected": [
            {"kind": "platform", "id": platform["entity"], "asic_families": list(platform["families"]),
             "arch": platform["arch"], "resolution": platform["resolution"],
             "job_groups": [group.split(":", 1)[-1] for group in platform["job_groups"]],
             "reason": _rule_reason(platform)}
            for platform in platforms
        ],
        "coverage_gap": _coverage_gap(view),
        "verification": dict(VERIFICATION),
        "backtest_ref": None,
    }


def _band(question: Dict[str, Any], result: QuestionResult, kept: Sequence[Any], contested: bool = False,
          undecided: bool = False) -> Confidence:
    required = list(question.get("required_evidence") or ())
    answers = [(tuple(item.role for item in answer.citations), len(answer.members)) for answer in kept]
    return band_adjudication(float(question.get("prior", 0.0)), required, answers, contested, undecided)


def _confidence(confidence: Confidence) -> Dict[str, Any]:
    if confidence.band in (BAND_HIGH, BAND_MEDIUM):
        return {"band": confidence.band, "score": confidence.score, "basis": "adjudication"}
    return {"band": BAND_PROVEN, "score": 1.0, "basis": "deterministic"}


def _adjudication(result: QuestionResult, kept: Sequence[Any], confidence: Confidence, resolution: Optional[str],
                  agent: AgentResult, rationale: str) -> Dict[str, Any]:
    if kept:
        status = "contested" if any(item.outcome == OUTCOME_CONTESTED for item in kept) else "answered"
    elif result.status == Q_ANSWERED:
        status = "unanswered"
    else:
        status = result.status
    groups = {group.id: group for group in result.assembly.groups} if result.assembly else {}
    return {
        "question": result.id,
        "status": status,
        "resolution": resolution,
        "rationale": rationale,
        "band": confidence.band,
        "score": confidence.score,
        "band_reason": confidence.reason,
        "posted": False,
        "judgement": "model",
        "model": agent.model_id,
        "reason": result.reason,
        "answers": [_answer(item, groups.get(item.group)) for item in result.items],
    }


def _answer(item: Any, group: Optional[Group]) -> Dict[str, Any]:
    answer = dict(item.answer or {})
    payload: Dict[str, Any] = {
        "group": item.group,
        "members": list(item.members),
        "member_count": len(item.members),
        "outcome": item.outcome,
        "reason": str(answer.get("reason", "")),
        "cite": list(answer.get("cite", [])),
        "check": item.check,
        "detail": item.detail,
        "agrees_with_rule": item.agrees,
        "counter_evidence": item.counter_evidence,
        "rule": ({"resolution": group.rule_resolution, "job_groups": list(group.rule_job_groups)}
                 if group is not None and group.rule_resolution else None),
    }
    if "verdict" in answer:
        payload["verdict"] = answer["verdict"]
    if "covered" in answer:
        payload["covered"] = bool(answer["covered"])
        payload["job_group"] = str(answer.get("job_group", ""))
    return payload


def _rationale(result: QuestionResult, kept: Sequence[Any], says: Any) -> str:
    parts = []
    for item in kept:
        what = says(item)
        reason = str((item.answer or {}).get("reason", ""))
        parts.append(f"{item.group} ({len(item.members)} platform(s)): {what}. {reason}".strip())
    return " ".join(parts)


def _covered_phrase(item: Any) -> str:
    answer = item.answer or {}
    said = f"covered by {answer.get('job_group')}" if answer.get("covered") else "not covered"
    return f"{item.outcome}, {said}"


def _materiality_resolution(kept: Sequence[Any], confidence: Confidence) -> Optional[str]:
    verdicts = _verdicts(kept)
    if not verdicts:
        return None
    if VERDICT_MATERIAL in verdicts:
        return VERDICT_MATERIAL
    if verdicts == {VERDICT_PASS_THROUGH}:
        return VERDICT_PASS_THROUGH
    return VERDICT_UNCLEAR


def _verdicts(kept: Sequence[Any]) -> set:
    return {str((item.answer or {}).get("verdict", "")) for item in kept if (item.answer or {}).get("verdict")}


def _evidence(result: QuestionResult, kept: Sequence[Any]) -> List[Dict[str, Any]]:
    """What the answers cited, then the deterministic backing: the first cause, affected and contract."""
    if result.assembly is None:
        return []
    chosen: Dict[str, Evidence] = {}
    for item in kept:
        for evidence in item.citations:
            chosen.setdefault(evidence.id, evidence)
    for role in (ROLE_CAUSE, ROLE_AFFECTED, ROLE_CONTRACT):
        first = next((item for item in result.assembly.book.items if item.role == role), None)
        if first is not None:
            chosen.setdefault(first.id, first)
    ordered = sorted(chosen.values(), key=lambda item: int(item.id[1:]) if item.id[1:].isdigit() else 0)
    return [dict(item.citation(), id=item.id, label=item.label) for item in ordered[:MAX_EVIDENCE]]


def _families(result: QuestionResult, platforms: Sequence[str]) -> Dict[str, Tuple[str, ...]]:
    families: Dict[str, Tuple[str, ...]] = {}
    if result.assembly is not None:
        for group in result.assembly.groups:
            for member in group.members:
                families[member] = tuple(group.families)
    return {entity: families[entity] for entity in platforms if entity in families}


def _never_built(view: BriefView) -> int:
    for item in view.unresolved.values():
        candidate = item.get("rule_candidate")
        if candidate:
            return int(candidate["uncovered"])
    return len(view.coverage["uncovered"])


def _coverage_gap(view: BriefView) -> Dict[str, Any]:
    coverage = view.coverage
    return {
        "declarations_in_tree": coverage["declarations_in_tree"],
        "platforms_in_tree": coverage["platforms_in_tree"],
        "job_groups": coverage["job_groups"],
        "affected_count": len(coverage["affected"]),
        "covered_by_pr_ci": len(coverage["covered"]),
        "uncovered": len(coverage["uncovered"]),
        "ambiguous": len(coverage["ambiguous"]),
        "never_built": _never_built(view),
    }


def _arch(view: BriefView, entity_id: str) -> str:
    return str(view.entities.get(entity_id, {}).get("arch", ""))


def _rule_reason(platform: Dict[str, Any]) -> str:
    families = ", ".join(platform["families"])
    if platform["resolution"] == "covered":
        groups = ", ".join(group.split(":", 1)[-1] for group in platform["job_groups"])
        return f"{groups} builds {families} for {platform['arch']}, the platform's own architecture"
    if platform["resolution"] == "uncovered":
        return f"no PR-CI job group builds {families} for {platform['arch']}"
    return "the directory name follows no architecture prefix the rule knows"


def _rank(finding: Dict[str, Any]) -> Tuple[Any, ...]:
    adjudication = finding["adjudication"]
    return (-SEVERITY_RANK[finding["severity"]], -BAND_RANK[adjudication["band"]],
            -finding["deterministic"]["never_built"], finding["question"])


def _check_contracts(report: Dict[str, Any], brief: Dict[str, Any]) -> None:
    problems: List[str] = []
    known = {entity["id"] for entity in brief["entities"]}
    questions = {question["id"] for question in brief["questions"]}
    coverage = brief["coverage"]

    for finding in report["findings"]:
        where = f"finding {finding['id']}"
        if finding["question"] not in questions:
            problems.append(f"{where}: answers {finding['question']}, which the brief does not ask")
        named = {item["id"] for item in finding["affected"]}
        named |= {member for answer in finding["adjudication"]["answers"] for member in answer.get("members", [])}
        outside = sorted(named - known)
        if outside:
            problems.append(f"{where}: names entities outside the brief: {outside}")
        adjudication = finding["adjudication"]
        contested = any(answer["outcome"] == OUTCOME_CONTESTED for answer in adjudication["answers"])
        if contested and BAND_RANK[adjudication["band"]] > BAND_RANK[BAND_MEDIUM]:
            problems.append(f"{where}: a contested adjudication is banded {adjudication['band']}, above medium")
        confidence = finding["confidence"]
        if confidence["basis"] == "adjudication" and confidence["band"] != adjudication["band"]:
            problems.append(f"{where}: ranks on an adjudication banded {adjudication['band']}, "
                            f"not {confidence['band']}")
        if adjudication.get("posted") and adjudication["band"] not in (BAND_HIGH, BAND_MEDIUM):
            problems.append(f"{where}: posts an adjudication banded {adjudication['band']}")
        for answer in adjudication["answers"]:
            if answer["outcome"] in (OUTCOME_DROPPED,) and not answer["check"]:
                problems.append(f"{where}: drops group {answer['group']} without naming the check")
            if answer["outcome"] == OUTCOME_UNEXAMINED and answer["group"]:
                problems.append(f"{where}: marks named group {answer['group']} unexamined")
        gap = finding["coverage_gap"]
        if (gap["affected_count"], gap["covered_by_pr_ci"], gap["ambiguous"]) != (
                len(coverage["affected"]), len(coverage["covered"]), len(coverage["ambiguous"])):
            problems.append(f"{where}: coverage numbers differ from the brief's")

    if report["run"]["status"] == "degraded" and not report["degraded_reason"]:
        problems.append("run: degraded without a degraded_reason")
    if report["run"]["questions_answered"] > report["run"]["questions_asked"]:
        problems.append("run: more questions answered than asked")
    for question in report["run"].get("questions", []):
        if question["status"] == Q_TRUNCATED and question["id"] not in report["run"]["truncated"]:
            problems.append(f"run: {question['id']} is truncated but not listed as such")

    if problems:
        raise ReportContractError(problems)
