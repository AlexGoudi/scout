"""`scout-run.jsonl`: one structured record per stage boundary, model call, tool call and read.

HLD section 6.7. The run log is the trace behind the report's summary numbers, and the
place NFR-9 is made auditable: every excerpt that left the trust boundary for the model is
named in the `model_call` record that sent it, by path, revision and line range.

Records are appended in order with a sequence number and an offset from the start of the
run rather than a wall-clock timestamp, so two logs of the same run differ only in how long
things took. When a path is given each record is also written as it happens, so a run that
is killed part-way still leaves the trace of what it did.
"""

import json
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


class RunLog:
    """An append-only list of records, optionally streamed to a JSONL file."""

    def __init__(self, path: Optional[Path] = None, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._started = clock()
        self._records: List[Dict[str, Any]] = []
        self._path = Path(path) if path is not None else None
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text("", encoding="utf-8")

    @property
    def records(self) -> List[Dict[str, Any]]:
        return list(self._records)

    def elapsed(self) -> float:
        return self._clock() - self._started

    def emit(self, event: str, **fields: Any) -> Dict[str, Any]:
        record = {"seq": len(self._records) + 1, "t": round(self.elapsed(), 3), "event": event}
        record.update(fields)
        self._records.append(record)
        if self._path is not None:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        return record

    def events(self, event: str) -> List[Dict[str, Any]]:
        return [record for record in self._records if record["event"] == event]

    def count(self, event: str, **match: Any) -> int:
        return sum(1 for record in self.events(event)
                   if all(record.get(key) == value for key, value in match.items()))

    def counter(self, event: str, key: str) -> Dict[str, int]:
        return dict(Counter(str(record.get(key)) for record in self.events(event)))

    def write(self, path: Path) -> None:
        """Write every record so far, for a log that was kept in memory."""
        lines = [json.dumps(record, sort_keys=True, default=str) for record in self._records]
        Path(path).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
