"""Stage 2, the agent stage bounded by the brief.

The static stage decided what is in play and wrote the questions down; this package asks
them, and nothing else. The model is given pre-assembled evidence and a form to fill in,
and five constraints hold it to the brief. Every one is enforced in code on what comes
back, never requested in a prompt and trusted:

| Constraint | Where | On failure |
| --- | --- | --- |
| Entity closure | `checks.ClosedWorld` | The answer naming an outside entity is dropped |
| Question binding | `runner`, over `protocol.Reply` | An answer to no group of the question is dropped and counted |
| Budget | `budget.Budget` | The question is marked `truncated`; the run carries on |
| Rule consistency | `checks.rule_consistency` | Disagreeing with `rule_candidate` marks the answer `contested` |
| Cited job group | `checks.cited_job_group` | A "covered" claim whose group does not match is dropped |

The citation resolver (`checks.CitationResolver`) runs last over what survives, and re-reads every
quote an answer rests on at the revision it names.

`run_agent` is the entry point. It never raises for anything a model or a budget can do:
no provider, an unreachable one, or an exhausted run budget all yield a `degraded` result
carrying whatever was answered, and a brief with no questions costs no model call at all.
"""

from .runner import AgentResult, ItemResult, QuestionResult, run_agent

__all__ = ["AgentResult", "ItemResult", "QuestionResult", "run_agent"]
