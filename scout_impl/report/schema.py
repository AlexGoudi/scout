"""The report's schema, validated with the same stdlib validator as the brief's.

Reusing `static/schema.py` rather than a second validator keeps one rule for both
contracts: a keyword the validator does not implement is rejected, never skipped, so the
report schema cannot grow a constraint that silently goes unchecked.
"""

from pathlib import Path
from typing import Any, Dict, Optional

from ..static.schema import load_schema, validate

REPORT_SCHEMA_VERSION = "2.0"


def report_schema(directory: Optional[Path] = None) -> Dict[str, Any]:
    return load_schema(f"scout-report-{REPORT_SCHEMA_VERSION}.json", directory)


def validate_report(document: Any, directory: Optional[Path] = None) -> None:
    validate(document, report_schema(directory))
