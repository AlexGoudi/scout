"""The budget governor: what stage 2 may spend, per question and per run (HLD sections 4.5, 6.4).

Two layers, charged together. Each question carries its own tool-call and blob-read budget
in the brief, and gets a share of the run's input tokens; the run carries the brief's
`budget` block and a wall-clock deadline (NFR-2's 20-minute hard timeout). A charge that
would exceed either layer raises `BudgetExhausted` naming which one, so the caller can tell
"truncate this question and carry on" from "stop asking and report what is known".

Blob reads are charged when they cost a round trip and never when the blob cache already
holds the bytes, which is the figure NFR-12 counts. Input tokens are charged at the larger
of what the provider reports and an estimate from the prompt's length, because a provider
that caches a shared prompt prefix reports only the tokens it evaluated, and a budget that
trusted that figure would let a long conversation look cheap.
"""

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

TOOL_CALLS = "tool_calls"
BLOB_READS = "blob_reads"
INPUT_TOKENS = "input_tokens"
WALL_CLOCK = "wall_clock"

SCOPE_QUESTION = "question"
SCOPE_RUN = "run"

DEFAULT_DEADLINE_S = 20 * 60.0


class BudgetExhausted(RuntimeError):
    """A charge would exceed a budget. `scope` says whether it was the question's or the run's."""

    def __init__(self, scope: str, kind: str, limit: float, used: float, wanted: float) -> None:
        self.scope = scope
        self.kind = kind
        self.limit = limit
        self.used = used
        self.wanted = wanted
        super().__init__(f"{scope} budget for {kind} exhausted: {used:g} used of {limit:g}, {wanted:g} more wanted")


@dataclass
class Meter:
    """One capped counter."""

    limit: float
    used: float = 0

    @property
    def remaining(self) -> float:
        return max(0, self.limit - self.used)

    def fits(self, amount: float) -> bool:
        return self.used + amount <= self.limit


@dataclass
class QuestionBudget:
    """The meters one question is charged against."""

    question: str
    meters: Dict[str, Meter] = field(default_factory=dict)

    def used(self, kind: str) -> float:
        meter = self.meters.get(kind)
        return meter.used if meter else 0

    def remaining(self, kind: str) -> float:
        meter = self.meters.get(kind)
        return meter.remaining if meter else float("inf")

    def to_dict(self) -> Dict[str, Dict[str, float]]:
        return {kind: {"limit": meter.limit, "used": meter.used} for kind, meter in sorted(self.meters.items())}


def estimate_tokens(text: str) -> int:
    """A conservative token estimate for text sent to the model: about three characters each."""
    return len(text) // 3 + 1


class Budget:
    """Run-level caps from the brief, and the per-question budgets carved out of them."""

    def __init__(
        self,
        brief_budget: Dict[str, int],
        question_count: int,
        deadline_s: float = DEFAULT_DEADLINE_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.run = {
            TOOL_CALLS: Meter(float(brief_budget.get("max_tool_calls", 0))),
            BLOB_READS: Meter(float(brief_budget.get("max_blob_reads", 0))),
            INPUT_TOKENS: Meter(float(brief_budget.get("max_input_tokens", 0))),
        }
        self.question_input_tokens = int(self.run[INPUT_TOKENS].limit // max(1, question_count))
        self.deadline_s = deadline_s
        self._clock = clock
        self._started = clock()

    def question(self, question_id: str, budget: Dict[str, int]) -> QuestionBudget:
        return QuestionBudget(
            question=question_id,
            meters={
                TOOL_CALLS: Meter(float(budget.get("tool_calls", 0))),
                BLOB_READS: Meter(float(budget.get("blob_reads", 0))),
                INPUT_TOKENS: Meter(float(self.question_input_tokens)),
            },
        )

    def elapsed(self) -> float:
        return self._clock() - self._started

    def check_deadline(self) -> None:
        if self.elapsed() > self.deadline_s:
            raise BudgetExhausted(SCOPE_RUN, WALL_CLOCK, self.deadline_s, round(self.elapsed(), 3), 0)

    def require(self, kind: str, amount: float, question: Optional[QuestionBudget] = None) -> None:
        """Raise if `amount` would not fit, without charging it."""
        if question is not None and kind in question.meters and not question.meters[kind].fits(amount):
            meter = question.meters[kind]
            raise BudgetExhausted(SCOPE_QUESTION, kind, meter.limit, meter.used, amount)
        if kind in self.run and not self.run[kind].fits(amount):
            meter = self.run[kind]
            raise BudgetExhausted(SCOPE_RUN, kind, meter.limit, meter.used, amount)

    def charge(self, kind: str, amount: float, question: Optional[QuestionBudget] = None) -> None:
        """Charge both layers, or neither: a refused charge leaves the meters as they were."""
        self.require(kind, amount, question)
        self.record(kind, amount, question)

    def record(self, kind: str, amount: float, question: Optional[QuestionBudget] = None) -> None:
        """Account for a cost already incurred, such as the tokens a completed call reports."""
        if question is not None and kind in question.meters:
            question.meters[kind].used += amount
        if kind in self.run:
            self.run[kind].used += amount

    def to_dict(self) -> Dict[str, Dict[str, float]]:
        summary = {kind: {"limit": meter.limit, "used": meter.used} for kind, meter in sorted(self.run.items())}
        summary[WALL_CLOCK] = {"limit": self.deadline_s, "used": round(self.elapsed(), 3)}
        return summary
