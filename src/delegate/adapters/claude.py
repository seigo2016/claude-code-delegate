"""Claude Code in headless mode, mapped onto the normalized event kinds.

Only the closing result event counts as the answer, so a worker that thinks out
loud and then stalls is a stall rather than a delivery.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from delegate.adapters.base import NormalizedEvent

# A worker cannot answer an interactive permission prompt, and bypassing every
# check would let a bounded task do unbounded things.
PERMISSION_MODE = "acceptEdits"


class ClaudeAdapter:
    name = "claude"

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
            "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            model,
            "--effort",
            effort,
            "--permission-mode",
            PERMISSION_MODE,
        ]

    def parse_events(self, raw_line: str) -> list[NormalizedEvent]:
        try:
            event: Any = json.loads(raw_line)
        except json.JSONDecodeError:
            return []
        if not isinstance(event, dict):
            return []
        kind = event.get("type")

        if kind == "system" and event.get("subtype") == "init":
            session_id = event.get("session_id")
            return [
                NormalizedEvent(
                    kind="session_started",
                    session_id=session_id if isinstance(session_id, str) else None,
                )
            ]
        if kind == "result":
            text = event.get("result")
            events = [NormalizedEvent(kind="turn_completed")]
            if isinstance(text, str):
                events.insert(
                    0,
                    NormalizedEvent(kind="item_completed", item_type="agent_message", text=text),
                )
            return events
        if kind == "rate_limit_event":
            info = event.get("rate_limit_info")
            status = info.get("status") if isinstance(info, dict) else None
            if status != "allowed":
                return [NormalizedEvent(kind="runtime_warning", text=str(status))]
            return []
        if kind in {"assistant", "user"}:
            return _tool_events(event, kind)
        return []

    def parse_stderr_lines(self, raw_line: str) -> list[NormalizedEvent]:
        return []


def _tool_events(event: dict[str, Any], kind: str) -> list[NormalizedEvent]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    found = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if kind == "assistant" and block.get("type") == "tool_use":
            found.append(
                NormalizedEvent(
                    kind="item_started",
                    item_id=block.get("id") if isinstance(block.get("id"), str) else None,
                    item_type=block.get("name") if isinstance(block.get("name"), str) else "tool",
                )
            )
        elif kind == "user" and block.get("type") == "tool_result":
            found.append(
                NormalizedEvent(
                    kind="item_completed",
                    item_id=block.get("tool_use_id")
                    if isinstance(block.get("tool_use_id"), str)
                    else None,
                    item_type="tool",
                )
            )
    return found
