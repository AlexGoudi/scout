"""Orchestration: the repo-agnostic glue that drives a run end to end.

Thin by design. The orchestrator's contract is that a brief and a
report are always produced — including when no model was reachable, which for stage 1 is
every run — so what lives here is sequencing, measurement and the guarantee that the
artifact lands, not analysis.
"""
