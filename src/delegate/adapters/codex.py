"""Codex CLI, mapped onto the normalized event kinds."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from delegate.adapters.base import NormalizedEvent

# Codex retries its model list in the background; repeated timeouts there mean
# the run is stuck rather than working.
MODEL_REFRESH_TIMEOUT = (
    "codex_models_manager::manager: failed to refresh available models: "
    "timeout waiting for child process to exit"
)


class CodexAdapter:
    name = "codex"

    def build_command(
        self,
        *,
        project_root: Path,
        prompt_path: Path,
        result_path: Path,
        schema_path: Path,
        model: str,
        effort: str,
    ) -> list[str]:
        return [
            "codex",
            "exec",
            "-C",
            str(project_root),
            "--json",
            "-o",
            str(result_path),
            "--model",
            model,
            "-c",
            f'model_reasoning_effort="{effort}"',
            "--output-schema",
            str(schema_path),
            "--disable",
            "fast_mode",
            "-",
        ]

    def parse_events(self, raw_line: str) -> list[NormalizedEvent]:
        try:
            event: Any = json.loads(raw_line)
        except json.JSONDecodeError:
            return []
        if not isinstance(event, dict):
            return []

        kind = event.get("type")
        if kind == "thread.started":
            session_id = event.get("thread_id")
            return [
                NormalizedEvent(
                    kind="session_started",
                    session_id=session_id if isinstance(session_id, str) else None,
                )
            ]
        if kind == "turn.completed":
            return [NormalizedEvent(kind="turn_completed")]
        if kind in {"item.started", "item.completed"}:
            item = event.get("item")
            if not isinstance(item, dict):
                return []
            text = item.get("text")
            return [
                NormalizedEvent(
                    kind="item_started" if kind == "item.started" else "item_completed",
                    item_id=item.get("id") if isinstance(item.get("id"), str) else None,
                    item_type=item.get("type") if isinstance(item.get("type"), str) else None,
                    text=text if isinstance(text, str) else None,
                )
            ]
        return []

    def parse_stderr_lines(self, raw_line: str) -> list[NormalizedEvent]:
        if MODEL_REFRESH_TIMEOUT in raw_line:
            return [NormalizedEvent(kind="runtime_warning", text="model_refresh_timeout")]
        return []
