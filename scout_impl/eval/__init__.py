"""The evaluation harness: seed corpus, backtest runner and scorecard.

Nothing here is imported by the stages it measures. The static stage is driven through its
public functions, and the agent stage only through `agent_adapter`, which is the single
module allowed to reach into `scout_impl/agent/`, `scout_impl/report/` and
`scout_impl/core/review.py`, and which imports none of them until an agent-inclusive run
asks it to.
"""
