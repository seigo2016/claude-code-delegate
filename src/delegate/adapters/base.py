"""What every worker backend must look like from the broker's side.

Stall diagnosis reads only these kinds, so adding a backend cannot change how
failures are classified. One line of output can mean two things, hence the list.
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
)


@dataclass(frozen=True)
class NormalizedEvent:
    kind: str
    item_id: str | None = None
    item_type: str | None = None
    text: str | None = None
    session_id: str | None = None
    changed_paths: tuple[str, ...] = ()
    succeeded: bool | None = None


class WorkerAdapter(Protocol):
    name: str

    def build_command(
        self,
        *,
        project_root: Path,
        prompt_path: Path,
        result_path: Path,
        schema_path: Path,
        model: str,
        effort: str,
        writes_allowed: bool,
        runs_commands: bool,
    ) -> list[str]: ...

    def parse_events(self, raw_line: str) -> list[NormalizedEvent]: ...

    def parse_stderr_lines(self, raw_line: str) -> list[NormalizedEvent]: ...
