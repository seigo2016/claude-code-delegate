"""OpenCode CLI, mapped onto the normalized event kinds.

It has no output-file flag, and its step boundary repeats within a run, so the
answer comes from the last text part and nothing here means "turn over".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from delegate.adapters.base import NormalizedEvent

UNFINISHED = {"pending", "running"}


class OpenCodeAdapter:
    name = "opencode"

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
    ) -> list[str]:
        # opencode has no per-run permission flag, and was measured writing a file
        # and running a shell command without `--auto`, the flag its own help calls
        # dangerous. So a read-only task is read-only here by instruction only.
        del writes_allowed, runs_commands
        return [
            "opencode",
            "run",
            "--format",
            "json",
            "--dir",
            str(project_root),
            "-m",
            model,
            "--variant",
            effort,
            prompt_path.read_text(encoding="utf-8"),
        ]

    def parse_events(self, raw_line: str) -> list[NormalizedEvent]:
        try:
            event: Any = json.loads(raw_line)
        except json.JSONDecodeError:
            return []
        if not isinstance(event, dict):
            return []
        part = event.get("part")
        if not isinstance(part, dict):
            return []

        kind = event.get("type")
        session_id = event.get("sessionID")
        if kind == "step_start":
            return [
                NormalizedEvent(
                    kind="session_started",
                    session_id=session_id if isinstance(session_id, str) else None,
                )
            ]
        if kind == "text":
            text = part.get("text")
            if not isinstance(text, str):
                return []
            return [
                NormalizedEvent(
                    kind="item_completed",
                    item_id=part.get("id") if isinstance(part.get("id"), str) else None,
                    item_type="agent_message",
                    text=text,
                )
            ]
        if kind == "tool_use":
            state = part.get("state")
            status = state.get("status") if isinstance(state, dict) else None
            return [
                NormalizedEvent(
                    kind="item_started" if status in UNFINISHED else "item_completed",
                    item_id=part.get("id") if isinstance(part.get("id"), str) else None,
                    item_type=part.get("tool") if isinstance(part.get("tool"), str) else "tool",
                )
            ]
        return []

    def parse_stderr_lines(self, raw_line: str) -> list[NormalizedEvent]:
        return []
