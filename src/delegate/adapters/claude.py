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
# check would let a bounded task do unbounded things. Of the modes on offer, this
# is the only one that does neither: a command with no matching rule is classified
# rather than put to a person who is not there, or waved through.
PERMISSION_MODE = "auto"

#: Withholding these is enough: the classifier was measured turning down the ways
#: round them, a shell redirect and `tee`.
EDITING_TOOLS = ("Edit", "Write", "NotebookEdit")


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
        writes_allowed: bool,
        runs_commands: bool,
    ) -> list[str]:
        # Nothing here stands on a filesystem the way codex does, so running commands
        # costs nothing to allow and buys no editing tools.
        del runs_commands
        command = [
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
        if writes_allowed:
            # Holding the tool is not permission to use it: under `auto` each write
            # was still put to someone, and a task that declared where it would
            # write was refused there. Which paths it may touch is answered by the
            # scope check afterwards; a per-path allowance was tried and matched
            # nothing.
            command.extend(["--allowedTools", *EDITING_TOOLS])
        else:
            command.extend(["--disallowed-tools", *EDITING_TOOLS])
        return command

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
            # A run that was refused things still ends `end_turn` with is_error false,
            # so the refusals have to be carried out or the answer arrives looking as
            # though the worker did everything it set out to do.
            denials = event.get("permission_denials")
            if isinstance(denials, list):
                events[:0] = [
                    NormalizedEvent(kind="runtime_warning", text="permission_denied")
                    for _ in denials
                ]
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
