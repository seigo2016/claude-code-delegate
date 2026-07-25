"""What every worker backend must look like from the broker's side.

The broker never reads a backend's own event format. Each adapter maps its
backend onto these few kinds, and the stall diagnosis works off those alone, so
adding a backend cannot change how failures are classified.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

KINDS = (
    "session_started",
    "item_started",
    "item_completed",
    "turn_completed",
    "runtime_warning",
    "error",
)


@dataclass(frozen=True)
class NormalizedEvent:
    kind: str
    item_id: str | None = None
    item_type: str | None = None
    text: str | None = None
    session_id: str | None = None


class WorkerAdapter(Protocol):
    name: str

    def build_command(
        self,
        *,
        project_root: Path,
        result_path: Path,
        schema_path: Path,
        model: str,
        effort: str,
        resume_session_id: str | None,
    ) -> list[str]: ...

    def parse_event(self, raw_line: str) -> NormalizedEvent | None: ...

    def parse_stderr_line(self, raw_line: str) -> NormalizedEvent | None: ...
