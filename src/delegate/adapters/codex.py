"""Codex CLI, mapped onto the normalized event kinds."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from delegate.adapters.base import NormalizedEvent

# Codex retries its model list in the background; when that retry times out
# repeatedly the run is stuck rather than working, which is what we report.
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
        result_path: Path,
        schema_path: Path,
        model: str,
        effort: str,
        resume_session_id: str | None,
    ) -> list[str]:
        command = ["codex", "exec"]
        if resume_session_id:
            command.append("resume")
        else:
            command.extend(["-C", str(project_root)])
        command.extend(
            [
                "--json",
                "-o",
                str(result_path),
                "--model",
                model,
                "-c",
                f'model_reasoning_effort="{effort}"',
                "--output-schema",
                str(schema_path),
            ]
        )
        command.extend(["--disable", "fast_mode"])
        if resume_session_id:
            command.append(resume_session_id)
        command.append("-")
        return command

    def parse_event(self, raw_line: str) -> NormalizedEvent | None:
        try:
            event: Any = json.loads(raw_line)
        except json.JSONDecodeError:
            return None
        if not isinstance(event, dict):
            return None

        kind = event.get("type")
        if kind == "thread.started":
            session_id = event.get("thread_id")
            return NormalizedEvent(
                kind="session_started",
                session_id=session_id if isinstance(session_id, str) else None,
            )
        if kind == "turn.completed":
            return NormalizedEvent(kind="turn_completed")
        if kind in {"item.started", "item.completed"}:
            item = event.get("item")
            if not isinstance(item, dict):
                return None
            text = item.get("text")
            return NormalizedEvent(
                kind="item_started" if kind == "item.started" else "item_completed",
                item_id=item.get("id") if isinstance(item.get("id"), str) else None,
                item_type=item.get("type") if isinstance(item.get("type"), str) else None,
                text=text if isinstance(text, str) else None,
            )
        return None

    def parse_stderr_line(self, raw_line: str) -> NormalizedEvent | None:
        if MODEL_REFRESH_TIMEOUT in raw_line:
            return NormalizedEvent(kind="runtime_warning", text="model_refresh_timeout")
        return None
