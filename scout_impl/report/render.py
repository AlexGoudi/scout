"""Render the advisory comment from `scout-report.json`.

The one other input is the brief's optional `paths_to_assess` block, a fact of the static
stage the report does not carry; without a brief, or without that block, the comment is
exactly what the report alone renders.

Per finding: a one-line title, the deterministic statement with its numbers, the snippet it
rests on, the affected-platform count with the first few named, and then the model's
adjudication, set apart and labelled as judgement rather than fact. Where the brief's rule
answered a question, the rule's answer and the model's are shown side by side, with what the
checks made of the model's; the rule's answer is always the one that stands.

The comment is capped the way NFR-4 caps the report: at most `MAX_FINDINGS` findings, a
few names per list, a few lines per snippet, and `MAX_CHARS` in all. An adjudication the
report banded `low` is not shown, only noted, because `scoring.posts` suppresses it.
"""

from typing import Any, Dict, List, Optional, Sequence, Union

from ..static.brief import Brief
from .builder import Report

MAX_FINDINGS = 10
MAX_NAMED = 5
MAX_PATHS = 10
MAX_SNIPPET_LINES = 8
MAX_CHARS = 16000

CHECK_NAMES = {
    "question_binding": "question binding",
    "entity_closure": "entity closure",
    "cited_job_group": "cited job group",
    "citation": "citation",
    "rule_consistency": "rule consistency",
}


def render_comment(report: Union[Report, Dict[str, Any]],
                   brief: Optional[Union[Brief, Dict[str, Any]]] = None) -> str:
    payload = report.payload if isinstance(report, Report) else report
    brief_payload = brief.payload if isinstance(brief, Brief) else (brief or {})
    run = payload["run"]
    lines = ["## SONiC Scout: PR-CI coverage advisory", "",
             "> Advisory only: Scout never blocks a merge. **Facts** are computed from the tree and the pipeline "
             "definition. **Judgements** are a local model's reading of the evidence, checked in code but not "
             "facts.", ""]
    lines.append(_header(run))
    if payload["degraded_reason"]:
        lines.extend(["", f"> **Degraded:** {payload['degraded_reason']}. The facts below need no model; "
                          f"no judgement was made on them."])

    findings = payload["findings"][:MAX_FINDINGS]
    if not findings:
        lines.extend(["", _no_findings(run)])
    for index, finding in enumerate(findings, start=1):
        lines.extend([""] + _finding(index, finding))
    hidden = len(payload["findings"]) - len(findings)
    if hidden > 0:
        lines.extend(["", f"_{hidden} more finding(s) are in `scout-report.json`, beyond the cap of {MAX_FINDINGS}._"])
    if brief_payload.get("paths_to_assess"):
        lines.extend([""] + _paths_to_assess(brief_payload["paths_to_assess"]))
    lines.extend(["", _footer(run)])

    text = "\n".join(lines) + "\n"
    if len(text) > MAX_CHARS:
        cut = text[:MAX_CHARS].rsplit("\n", 1)[0]
        text = cut + f"\n\n_Truncated at {MAX_CHARS} characters; the whole report is `scout-report.json`._\n"
    return text


def _header(run: Dict[str, Any]) -> str:
    span = f"`{run['base_sha'][:9]}..{run['head_sha'][:9]}`" if run["base_sha"] else f"`{run['head_sha'][:9]}`"
    model = run["model"]["id"] or "none"
    return (f"`{run['repo']}` {span} ({run['mode']}) · detector D6, CI coverage gap · run **{run['status']}** · "
            f"model `{model}` · {run['questions_asked']} of {run['questions_in_brief']} question(s) put, "
            f"{run['questions_answered']} answered")


def _no_findings(run: Dict[str, Any]) -> str:
    if run["questions_in_brief"] == 0:
        return ("**No coverage gap.** Every platform this change reaches is built by a PR-CI job group, or it "
                "reaches none; no question needed a model.")
    return "**No finding.** The brief's questions produced nothing to report."


def _finding(index: int, finding: Dict[str, Any]) -> List[str]:
    deterministic = finding["deterministic"]
    lines = [f"### {index}. {finding['title']}", "",
             f"**Fact** (proven from the tree; severity {finding['severity']}): {deterministic['statement']}"]
    lines.extend(_snippet(finding["evidence"], prefer_contract="rule_candidate" in deterministic))
    lines.extend(["", _affected(finding)])
    if "rule_candidate" in deterministic:
        candidate = deterministic["rule_candidate"]
        lines.extend(["", f"**Rule answer** ({' and '.join(candidate['rules'])}, a naming convention, not a "
                          f"declaration; it stands unless contested with evidence): {candidate['covered']} built "
                          f"for their own architecture, {candidate['uncovered']} not."])
    lines.extend([""] + _judgement(finding))
    return lines


def _snippet(evidence: Sequence[Dict[str, Any]], prefer_contract: bool = False) -> List[str]:
    """The lines a reviewer should look at first: the change, or for a rule's answer, the job group it turns on."""
    cause = next((item for item in evidence if item["role"] == "cause" and item["rev"] == "head"), None)
    contract = (next((item for item in evidence if item["role"] == "contract" and "name:" in item["quote"]), None)
                or next((item for item in evidence if item["role"] == "contract"), None))
    ordered = (contract, cause) if prefer_contract else (cause, contract)
    chosen = next((item for item in ordered if item is not None), None) or next(iter(evidence), None)
    if chosen is None:
        return []
    rows = chosen["quote"].splitlines() or ["(empty)"]
    if len(rows) > MAX_SNIPPET_LINES:
        rows = rows[:MAX_SNIPPET_LINES] + [f"... {len(rows) - MAX_SNIPPET_LINES} more line(s)"]
    span = (str(chosen["line_start"]) if chosen["line_start"] == chosen["line_end"]
            else f"{chosen['line_start']}-{chosen['line_end']}")
    return ["", f"`{chosen['path']}` line(s) {span} at {chosen['rev']} ({chosen['role']}):", "", "```text", *rows,
            "```"]


def _affected(finding: Dict[str, Any]) -> str:
    affected = finding["affected"]
    unbuilt = [item for item in affected if item.get("resolution") != "covered"]
    names = [f"`{item['id'].split(':', 1)[1]}`" for item in unbuilt[:MAX_NAMED]]
    more = f", and {len(unbuilt) - MAX_NAMED} more" if len(unbuilt) > MAX_NAMED else ""
    families = sorted({family for item in unbuilt for family in item["asic_families"]})
    declared = f", declaring {', '.join(f'`{name}`' for name in families)}" if families else ""
    if not unbuilt:
        return f"**Never built by PR CI:** none of the {len(affected)} platform(s) this finding names."
    return f"**Never built by PR CI:** {len(unbuilt)} platform(s){declared}: {', '.join(names)}{more}."


def _judgement(finding: Dict[str, Any]) -> List[str]:
    adjudication = finding["adjudication"]
    model = f"`{adjudication['model']}`" if adjudication.get("model") else "the model"
    label = f"**Judgement** ({model} output, not a fact"
    if adjudication["band"] is None:
        why = adjudication.get("reason") or adjudication["status"]
        return [f"{label}): none. {_sentence(why)}"]
    if not adjudication.get("posted"):
        return [f"{label}): withheld, confidence {adjudication['band']}. {_sentence(adjudication['band_reason'])} "
                f"It is kept in `scout-report.json`."]

    answers = adjudication["answers"]
    ruled = any(answer.get("rule") for answer in answers)
    if ruled:
        groups = [answer for answer in answers if answer["group"]]
        confirmed = sum(1 for answer in groups if answer["outcome"] == "confirmed")
        headline = (f"**the model confirms the rule's answer for {confirmed} of {len(groups)} group(s)**, "
                    f"and the rule's answer stands")
    else:
        headline = f"**{adjudication['resolution']}**"
    lines = [f"{label}; confidence {adjudication['band']}, score {adjudication['score']}): {headline}. "
             f"{_sentence(adjudication['band_reason'])}"]
    if ruled:
        lines.extend(["", "| Group | Platforms | Rule answer | Model answer | Outcome |",
                      "| --- | --- | --- | --- | --- |"])
        for answer in answers:
            if not answer["group"]:
                continue
            lines.append(f"| {answer['group']} | {answer['member_count']} | {_rule_answer(answer)} | "
                         f"{_model_answer(answer)} | {_outcome(answer)} |")
    else:
        for answer in answers:
            if not answer["group"]:
                continue
            said = answer.get("verdict") or "no answer"
            quoted = f" \"{answer['reason']}\"" if answer.get("reason") else ""
            cites = f" (cites {', '.join(answer['cite'])})" if answer.get("cite") else ""
            lines.append(f"- Group {answer['group']}, {answer['member_count']} platform(s): **{said}**, "
                         f"{_outcome(answer)}.{quoted}{cites}")
    return lines


def _rule_answer(answer: Dict[str, Any]) -> str:
    rule = answer.get("rule") or {}
    if rule.get("resolution") == "covered":
        return f"covered by `{', '.join(rule.get('job_groups') or [])}`"
    return "not covered" if rule.get("resolution") == "uncovered" else "undetermined"


def _model_answer(answer: Dict[str, Any]) -> str:
    if "covered" not in answer:
        return "no answer"
    said = f"covered by `{answer['job_group'] or '?'}`" if answer["covered"] else "not covered"
    reason = answer.get("reason") or ""
    return f"{said}: \"{reason}\"" if reason else said


def _outcome(answer: Dict[str, Any]) -> str:
    outcome = answer["outcome"]
    if outcome == "dropped":
        return f"dropped by the {CHECK_NAMES.get(answer['check'], answer['check'])} check: {answer['detail']}"
    if outcome == "contested":
        weight = "" if answer.get("counter_evidence") else " without counter-evidence"
        return f"contested{weight}; the rule's answer stands"
    if outcome == "confirmed" and answer.get("covered"):
        return "confirmed; its job group checks out for family and architecture"
    return outcome


def _paths_to_assess(related: Dict[str, Any]) -> List[str]:
    features = ", ".join(f"`{feature['id']}` ({len(feature['changed'])} changed)" for feature in related["features"])
    how = ("the groups every matched path shares" if related["rule"] == "shared"
           else "no group is shared, so every group touched")
    lines = ["### Paths to assess", "",
             f"**Fact** (feature map `datapath.json`; {how}): this change touches {features}. Other paths in "
             f"the same feature group(s), worth checking alongside it:"]
    for item in related["repos"]:
        paths = item["paths"]
        named = ", ".join(f"`{path}`" for path in paths[:MAX_PATHS])
        more = f", and {len(paths) - MAX_PATHS} more in `scout-brief.json`" if len(paths) > MAX_PATHS else ""
        lines.append(f"- `{item['repo']}` ({len(paths)}): {named}{more}")
    return lines


def _footer(run: Dict[str, Any]) -> str:
    checks = run.get("checks") or {}
    fired = ", ".join(f"{CHECK_NAMES[name]} {count}" for name, count in sorted(checks.items()) if name in CHECK_NAMES)
    cost = run["cost"]
    return (f"<sub>Brief `{run['brief_sha'][:12]}` · prompt `{run['model']['prompt_sha'][:12]}` · "
            f"{run.get('model_calls', 0)} model call(s), {cost['input_tokens']} tokens in, "
            f"{cost['output_tokens']} out · checks fired: {fired or 'none'}.</sub>")


def _sentence(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    return text if text.endswith(".") else text + "."
