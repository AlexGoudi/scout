"""The agent loop: one bounded conversation per brief question, then the checks.

For each question, in brief order: assemble the groups and the evidence, put the question
once, allow at most `MAX_TOOL_STEPS` optional reads, and take the answer the schema forces
on the last step. Then every answer goes through the checks in `checks.py`, and what
survives is recorded beside what did not, with the check that stopped it. The loop never
decides anything a rule already decided, and never lets the model's text stand in for a
check.

Failure is shaped, never raised past `run_agent`:

| What happened | Result |
| --- | --- |
| The brief has no questions | No provider call at all, not even a preflight; `complete` |
| No provider, a failed preflight, or a provider error | `degraded`, with what was answered so far |
| A question's own budget, or the prompt outgrowing the window | That question `truncated`; `partial` |
| The run's budget or its wall-clock deadline | The rest `not-run`; `degraded` |
| A reply that is not the requested JSON | That question `unanswered`; `partial` |

The prompt-size cap is a constant rather than whatever the live provider reports, so a
replay truncates exactly where the recording did, and a replay key never misses because
the provider serving it knows less about windows than the one that recorded it.
"""

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from ..core.runlog import RunLog
from ..detectors.base import QUESTION_AMBIGUITY
from ..models import ChangeSet
from ..provider import ROLE_ASSISTANT, ROLE_SYSTEM, ROLE_USER, Message, Provider, ProviderError, TokenUsage
from ..source import RepoSource
from ..static.brief import Brief
from ..static.schema import validate_brief
from .assemble import Assembly, BriefView, assemble
from .budget import (
    DEFAULT_DEADLINE_S,
    INPUT_TOKENS,
    SCOPE_RUN,
    TOOL_CALLS,
    Budget,
    BudgetExhausted,
    QuestionBudget,
    estimate_tokens,
)
from .checks import (
    CHECK_CITATION,
    CHECK_CITED_JOB_GROUP,
    CHECK_ENTITY_CLOSURE,
    CHECK_QUESTION_BINDING,
    CHECK_RULE_CONSISTENCY,
    CHECKS,
    OUTCOME_ANSWERED,
    OUTCOME_CONFIRMED,
    OUTCOME_CONTESTED,
    OUTCOME_DROPPED,
    OUTCOME_UNANSWERED,
    OUTCOME_UNCONFIRMED,
    OUTCOME_UNEXAMINED,
    CitationResolver,
    ClosedWorld,
    cited_job_group,
    entity_closure,
    premises,
    rule_consistency,
)
from .evidence import (
    ROLE_AFFECTED,
    ROLE_CAUSE,
    ROLE_CONTRACT,
    Evidence,
    add_excerpt,
    add_grep_hits,
    add_listing,
    latest_diffs,
)
from .prompts import PROMPT_SHA, SYSTEM_PROMPT, render_observation, render_question
from .protocol import (
    ACTION_GREP,
    ACTION_LIST,
    ACTION_READ,
    MalformedReply,
    Reply,
    ToolRequest,
    parse_reply,
    response_format,
)
from .toolbox import ToolError, Toolbox

MAX_TOOL_STEPS = 2
MAX_PROMPT_TOKENS = 7000

STATUS_COMPLETE = "complete"
STATUS_PARTIAL = "partial"
STATUS_DEGRADED = "degraded"

Q_ANSWERED = "answered"
Q_TRUNCATED = "truncated"
Q_UNANSWERED = "unanswered"
Q_SKIPPED = "skipped"
Q_NOT_RUN = "not-run"

KEPT_OUTCOMES = (OUTCOME_ANSWERED, OUTCOME_CONFIRMED, OUTCOME_CONTESTED, OUTCOME_UNCONFIRMED)


class ContextExceeded(RuntimeError):
    """The next prompt would not fit the pinned window."""


@dataclass(frozen=True)
class CallRecord:
    """One model call, as the run log and the report's measurements see it."""

    step: int
    action: str
    input_tokens: int
    output_tokens: int
    latency_s: float
    cached: bool
    finish_reason: str
    timings: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"step": self.step, "action": self.action, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "latency_s": round(self.latency_s, 3),
                "cached": self.cached, "finish_reason": self.finish_reason, "timings": dict(self.timings)}


@dataclass(frozen=True)
class ItemResult:
    """One group's answer and what the checks made of it."""

    group: str
    members: Tuple[str, ...]
    outcome: str
    answer: Optional[Dict[str, Any]] = None
    citations: Tuple[Evidence, ...] = ()
    check: str = ""
    detail: str = ""
    agrees: Optional[bool] = None
    counter_evidence: bool = False

    @property
    def kept(self) -> bool:
        return self.outcome in KEPT_OUTCOMES


@dataclass
class QuestionResult:
    """Everything one question cost and produced."""

    question: Dict[str, Any]
    kind: str
    status: str
    assembly: Optional[Assembly] = None
    items: List[ItemResult] = field(default_factory=list)
    calls: List[CallRecord] = field(default_factory=list)
    checks: Counter = field(default_factory=Counter)
    reason: str = ""
    budget: Dict[str, Any] = field(default_factory=dict)
    tool_steps: int = 0

    @property
    def id(self) -> str:
        return str(self.question["id"])

    @property
    def kept(self) -> List[ItemResult]:
        return [item for item in self.items if item.kept]

    @property
    def latency_s(self) -> float:
        return sum(call.latency_s for call in self.calls)

    @property
    def usage(self) -> TokenUsage:
        return TokenUsage(input_tokens=sum(call.input_tokens for call in self.calls),
                          output_tokens=sum(call.output_tokens for call in self.calls))


@dataclass
class AgentResult:
    """Stage 2's whole output: per-question results plus what the run spent."""

    status: str
    questions: List[QuestionResult]
    provider: str = "none"
    model_id: str = ""
    prompt_sha: str = PROMPT_SHA
    replayed: bool = False
    degraded_reason: Optional[str] = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    usd: float = 0.0
    duration_s: float = 0.0
    blobs_read: int = 0
    budget: Dict[str, Any] = field(default_factory=dict)
    consulted: Dict[str, List[str]] = field(default_factory=dict)

    @property
    def asked(self) -> List[QuestionResult]:
        return [result for result in self.questions if result.calls]

    @property
    def answered(self) -> List[QuestionResult]:
        return [result for result in self.questions if result.status in (Q_ANSWERED, Q_TRUNCATED) and result.kept]

    @property
    def truncated(self) -> List[str]:
        return [result.id for result in self.questions if result.status == Q_TRUNCATED]

    @property
    def model_calls(self) -> int:
        return sum(len(result.calls) for result in self.questions)

    @property
    def checks(self) -> Dict[str, int]:
        total: Counter = Counter({name: 0 for name in CHECKS})
        for result in self.questions:
            total.update(result.checks)
        return dict(sorted(total.items()))

    def question(self, question_id: str) -> Optional[QuestionResult]:
        return next((result for result in self.questions if result.id == question_id), None)


def run_agent(
    brief: Union[Brief, Dict[str, Any]],
    source: RepoSource,
    change_set: Optional[ChangeSet] = None,
    provider: Optional[Provider] = None,
    run_log: Optional[RunLog] = None,
    deadline_s: float = DEFAULT_DEADLINE_S,
    clock: Callable[[], float] = time.monotonic,
) -> AgentResult:
    """Answer the brief's questions with `provider`, bounded by the brief. Never raises for the model's sake."""
    payload = brief.payload if isinstance(brief, Brief) else brief
    validate_brief(payload)
    view = BriefView(payload)
    log = run_log if run_log is not None else RunLog()
    started = clock()
    budget = Budget(payload["budget"], len(view.questions), deadline_s=deadline_s, clock=clock)
    toolbox = Toolbox(source, view.run["head_sha"], view.run["base_sha"], payload["entities"], budget, log)
    loop = _Loop(view, toolbox, change_set, provider, budget, log, clock)
    log.emit("stage_start", stage="agent", questions=len(view.questions),
             provider=provider.spec.provider if provider else "none")

    status, reason = STATUS_COMPLETE, None
    results: List[QuestionResult] = []
    if not view.questions:
        log.emit("agent_skipped", reason="the brief asks no questions, so no model is called")
    elif provider is None:
        status, reason = STATUS_DEGRADED, "no model provider was configured, so no question was put"
    else:
        try:
            provider.preflight()
        except ProviderError as error:
            status, reason = STATUS_DEGRADED, f"the model provider is unavailable: {error}"

    for question in view.questions:
        kind = view.question_kind(question)
        if reason is not None:
            results.append(loop.assemble_only(question, kind, reason))
            continue
        result = loop.answer(question, kind)
        results.append(result)
        if loop.stop_reason:
            status, reason = STATUS_DEGRADED, loop.stop_reason

    if status != STATUS_DEGRADED and any(result.status in (Q_TRUNCATED, Q_UNANSWERED) for result in results):
        status = STATUS_PARTIAL
    if reason:
        log.emit("degraded", reason=reason)

    # This run's calls, not the provider's lifetime total: a backtest reuses one provider.
    usage = TokenUsage(input_tokens=sum(item.usage.input_tokens for item in results),
                       output_tokens=sum(item.usage.output_tokens for item in results))
    result = AgentResult(
        status=status,
        questions=results,
        provider=provider.spec.provider if provider else "none",
        model_id=provider.spec.model_id if provider else "",
        replayed=any(call.cached for item in results for call in item.calls),
        degraded_reason=reason,
        usage=usage,
        usd=provider.spec.usd(usage) if provider else 0.0,
        duration_s=clock() - started,
        blobs_read=toolbox.blob_reads,
        budget=budget.to_dict(),
        consulted={toolbox.symbolic(commit): sorted(paths) for commit, paths in sorted(toolbox.consulted.items())},
    )
    log.emit("stage_end", stage="agent", status=status, model_calls=result.model_calls,
             input_tokens=result.usage.input_tokens, output_tokens=result.usage.output_tokens,
             blobs_read=result.blobs_read, checks=result.checks, duration_s=round(result.duration_s, 3))
    return result


class _Loop:
    """The per-question conversation, and the stop signal a run-level failure raises."""

    def __init__(self, view: BriefView, toolbox: Toolbox, change_set: Optional[ChangeSet], provider: Provider,
                 budget: Budget, log: RunLog, clock: Callable[[], float]) -> None:
        self.view = view
        self.toolbox = toolbox
        self.change_set = change_set
        self.provider = provider
        self.budget = budget
        self.log = log
        self.clock = clock
        self.stop_reason: Optional[str] = None
        self.changed = set(latest_diffs(change_set))

    def assemble_only(self, question: Dict[str, Any], kind: str, reason: str) -> QuestionResult:
        """A question that will not be put: its evidence is still gathered, so a degraded
        report cites the change and the declarations rather than bare counts."""
        result = QuestionResult(question=question, kind=kind, status=Q_NOT_RUN, reason=reason)
        quota = self.budget.question(question["id"], question["budget"])
        try:
            result.assembly = assemble(question, self.view, self.toolbox, self.change_set, quota)
        except (BudgetExhausted, ToolError) as error:
            self.log.emit("assembly_failed", question=result.id, reason=str(error))
        result.budget = quota.to_dict()
        return result

    def answer(self, question: Dict[str, Any], kind: str) -> QuestionResult:
        result = QuestionResult(question=question, kind=kind, status=Q_ANSWERED)
        quota = self.budget.question(question["id"], question["budget"])
        try:
            result.assembly = assemble(question, self.view, self.toolbox, self.change_set, quota)
            if result.assembly.skipped or not result.assembly.groups:
                result.status = Q_SKIPPED
                result.reason = result.assembly.skipped or "the question names no platform stage 2 can group"
            else:
                self.log.emit("question_start", question=result.id, kind=kind,
                              groups=[group.to_dict() for group in result.assembly.groups],
                              unexamined=len(result.assembly.unexamined), evidence=len(result.assembly.book.items))
                reply = self._converse(result, quota)
                if reply is not None:
                    result.items = self._judge(result, reply, quota)
                    if not result.kept:
                        result.status, result.reason = Q_UNANSWERED, "no answer survived the checks"
                if result.assembly.unexamined:
                    result.status = Q_TRUNCATED
                    result.reason = (f"{len(result.assembly.unexamined)} platform(s) beyond the cap of groups one "
                                     f"question may put were not examined")
                    result.items.append(ItemResult(group="", members=result.assembly.unexamined,
                                                   outcome=OUTCOME_UNEXAMINED))
        except BudgetExhausted as exhausted:
            result.status, result.reason = Q_TRUNCATED, str(exhausted)
            self.log.emit("budget_exhausted", question=result.id, scope=exhausted.scope, kind=exhausted.kind,
                          limit=exhausted.limit, used=exhausted.used)
            if exhausted.scope == SCOPE_RUN:
                self.stop_reason = f"the run's budget ran out on {result.id}: {exhausted}"
        except ContextExceeded as error:
            result.status, result.reason = Q_TRUNCATED, str(error)
        except ProviderError as error:
            result.status, result.reason = Q_UNANSWERED, f"the model provider failed: {error}"
            self.stop_reason = result.reason
        result.budget = quota.to_dict()
        self.log.emit("question_end", question=result.id, status=result.status, reason=result.reason,
                      outcomes=dict(Counter(item.outcome for item in result.items)),
                      checks=dict(result.checks), calls=len(result.calls), latency_s=round(result.latency_s, 3),
                      input_tokens=result.usage.input_tokens, output_tokens=result.usage.output_tokens)
        return result

    def _converse(self, result: QuestionResult, quota: QuestionBudget) -> Optional[Reply]:
        assembly = result.assembly
        steps = self._steps_left(quota, 0)
        messages = [Message(role=ROLE_SYSTEM, content=SYSTEM_PROMPT),
                    Message(role=ROLE_USER, content=render_question(assembly, self.view, steps))]
        sent = 0
        for step in range(MAX_TOOL_STEPS + 1):
            tools = steps > 0
            response = response_format(result.kind, tools)
            self.budget.check_deadline()
            estimate = estimate_tokens("".join(message.content for message in messages)
                                       + json.dumps(response.parameters))
            if estimate > MAX_PROMPT_TOKENS:
                raise ContextExceeded(f"the prompt for {result.id} would be about {estimate} tokens, over the "
                                      f"{MAX_PROMPT_TOKENS} the pinned window leaves for it")
            self.budget.require(INPUT_TOKENS, estimate, quota)

            began = self.clock()
            completion = self.provider.complete(messages, [response])
            latency = self.clock() - began
            self.budget.record(INPUT_TOKENS, max(completion.usage.input_tokens, estimate), quota)
            try:
                reply: Optional[Reply] = parse_reply(completion.text, result.kind, tools)
                action = reply.action
            except MalformedReply as error:
                reply, action = None, "malformed"
                result.status, result.reason = Q_UNANSWERED, f"the model's reply was unusable: {error}"
            timings = {} if completion.cached else _timings(self.provider)
            result.calls.append(CallRecord(step=step, action=action, input_tokens=completion.usage.input_tokens,
                                           output_tokens=completion.usage.output_tokens, latency_s=latency,
                                           cached=completion.cached, finish_reason=completion.finish_reason,
                                           timings=timings))
            excerpts = [{"id": item.id, "path": item.path, "rev": item.rev, "lines": item.span}
                        for item in assembly.book.items[sent:]]
            sent = len(assembly.book.items)
            self.log.emit("model_call", question=result.id, step=step, action=action, excerpts=excerpts,
                          input_tokens=completion.usage.input_tokens, output_tokens=completion.usage.output_tokens,
                          estimated_input_tokens=estimate, latency_s=round(latency, 3), cached=completion.cached,
                          finish_reason=completion.finish_reason, timings=timings, reply=completion.text)
            if reply is None or reply.tool is None:
                return reply

            self.budget.charge(TOOL_CALLS, 1, quota)
            result.tool_steps += 1
            evidence, error = self._tool(reply.tool, result, quota)
            steps = self._steps_left(quota, result.tool_steps)
            messages.append(Message(role=ROLE_ASSISTANT, content=completion.text))
            messages.append(Message(role=ROLE_USER,
                                    content=render_observation(reply.tool.describe(), evidence, error, steps)))
        return None

    def _steps_left(self, quota: QuestionBudget, used: int) -> int:
        allowed = MAX_TOOL_STEPS - used
        affordable = min(quota.remaining(TOOL_CALLS), self.budget.run[TOOL_CALLS].remaining)
        return int(max(0, min(allowed, affordable)))

    def _tool(self, request: ToolRequest, result: QuestionResult, quota: QuestionBudget) -> Tuple[List[Evidence], str]:
        book = result.assembly.book
        args = request.args
        rev = str(args.get("rev") or "head")
        try:
            if request.action == ACTION_READ:
                path = str(args.get("path") or "").strip().lstrip("/")
                if not path:
                    raise ToolError("read_blob needs the path of a file")
                excerpt = self.toolbox.read_blob(path, rev, int(args.get("start") or 1),
                                                 int(args["end"]) if args.get("end") else None, question=quota)
                evidence = [add_excerpt(book, excerpt, self._role(path),
                                        label=f"read on request, {excerpt.total_lines} lines in all")]
            elif request.action == ACTION_LIST:
                prefix = str(args.get("prefix") or args.get("path") or "").strip().strip("/")
                entries, total = self.toolbox.list_tree(prefix, rev)
                evidence = [add_listing(book, prefix, self.toolbox.symbolic(self.toolbox.commit(rev)), entries, total)]
            elif request.action == ACTION_GREP:
                hits, _ = self.toolbox.grep(str(args.get("pattern") or ""), str(args.get("glob") or ""), rev,
                                            question=quota)
                evidence = add_grep_hits(book, hits, self.toolbox.symbolic(self.toolbox.commit(rev)))
            else:
                raise ToolError(f"{request.action} is not a tool this stage offers")
        except ToolError as error:
            self.log.emit("tool_call", question=result.id, tool=request.action, args=args, ok=False, error=str(error))
            return [], str(error)
        self.log.emit("tool_call", question=result.id, tool=request.action, args=args, ok=True,
                      evidence=[item.id for item in evidence])
        return evidence, ""

    def _role(self, path: str) -> str:
        if path in self.changed:
            return ROLE_CAUSE
        if path == "azure-pipelines.yml" or path.startswith(".azure-pipelines/"):
            return ROLE_CONTRACT
        return ROLE_AFFECTED

    def _judge(self, result: QuestionResult, reply: Reply, quota: QuestionBudget) -> List[ItemResult]:
        assembly = result.assembly
        book = assembly.book
        shown = [text for item in book.items for text in (item.path, item.quote, item.label)]
        world = ClosedWorld(self.view.payload["entities"], shown)
        resolver = CitationResolver(self.toolbox, book)
        items: List[ItemResult] = []
        seen = set()
        for answer in reply.answers:
            group = assembly.group(answer.group)
            if group is None or answer.group in seen:
                why = (f"answers group {answer.group!r} a second time" if group is not None
                       else f"answers group {answer.group!r}, which the question did not ask about")
                items.append(self._drop(result, answer, (), CHECK_QUESTION_BINDING, why))
                continue
            seen.add(answer.group)
            agrees = None
            if result.kind == QUESTION_AMBIGUITY and group.rule_resolution in ("covered", "uncovered"):
                agrees = bool(answer.covered) == (group.rule_resolution == "covered")

            problem = entity_closure(answer, world)
            if problem:
                items.append(self._drop(result, answer, group.members, CHECK_ENTITY_CLOSURE, problem, agrees))
                continue
            if result.kind == QUESTION_AMBIGUITY:
                problem = cited_job_group(answer, group, world)
                if problem:
                    items.append(self._drop(result, answer, group.members, CHECK_CITED_JOB_GROUP, problem, agrees))
                    continue
            evidence, problem = resolver.resolve(answer.cite, quota)
            if problem:
                items.append(self._drop(result, answer, group.members, CHECK_CITATION, problem, agrees))
                continue

            outcome, detail, check, counter = OUTCOME_ANSWERED, "", "", False
            if result.kind == QUESTION_AMBIGUITY:
                outcome, detail = rule_consistency(answer, group)
                if outcome == OUTCOME_CONTESTED:
                    check = CHECK_RULE_CONSISTENCY
                    counter = bool(set(answer.cite) - premises(group, assembly.context))
                    result.checks[CHECK_RULE_CONSISTENCY] += 1
                    self.log.emit("check", question=result.id, check=check, group=group.id, detail=detail,
                                  counter_evidence=counter)
            items.append(ItemResult(group=group.id, members=group.members, outcome=outcome,
                                    answer=answer.to_dict(), citations=tuple(evidence), check=check, detail=detail,
                                    agrees=agrees, counter_evidence=counter))

        for group in assembly.groups:
            if group.id not in seen:
                items.append(ItemResult(group=group.id, members=group.members, outcome=OUTCOME_UNANSWERED,
                                        detail="the model gave no answer for this group"))
        return items

    def _drop(self, result: QuestionResult, answer: Any, members: Tuple[str, ...], check: str, detail: str,
              agrees: Optional[bool] = None) -> ItemResult:
        result.checks[check] += 1
        self.log.emit("check", question=result.id, check=check, group=answer.group, detail=detail)
        return ItemResult(group=answer.group, members=members, outcome=OUTCOME_DROPPED, answer=answer.to_dict(),
                          check=check, detail=detail, agrees=agrees)


def _timings(provider: Provider) -> Dict[str, float]:
    """The server's own split of one call's time, where the provider (or the one it wraps) reports it."""
    for candidate in (provider, getattr(provider, "delegate", None)):
        found = getattr(candidate, "last_timings", None)
        if found:
            return {name: round(float(value), 3) for name, value in found.items()}
    return {}


__all__ = [
    "AgentResult",
    "CallRecord",
    "ItemResult",
    "QuestionResult",
    "MAX_TOOL_STEPS",
    "OUTCOME_DROPPED",
    "STATUS_COMPLETE",
    "STATUS_DEGRADED",
    "STATUS_PARTIAL",
    "run_agent",
]
