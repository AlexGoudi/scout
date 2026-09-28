"""Stage 4, the report: `scout-report.json` and the advisory comment rendered from it (HLD 4.10).

The JSON is the single source of truth, versioned and validated on write against
`schemas/scout-report-2.0.json` plus the contracts a schema cannot state; the comment is a
rendering of it and nothing else. Each finding keeps its two halves apart all the way to
the page: the deterministic statement, proven from the tree, and the model's adjudication,
banded separately and marked as judgement, so a reviewer can disagree with the judgement
without disbelieving the arithmetic.
"""

from .builder import Report, ReportContractError, build_report
from .render import render_comment
from .schema import validate_report

__all__ = ["Report", "ReportContractError", "build_report", "render_comment", "validate_report"]
