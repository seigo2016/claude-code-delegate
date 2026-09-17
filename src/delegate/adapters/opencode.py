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

READ_ONLY_AGENT = "claude-code-delegate-readonly"
READ_ONLY_CONFIG = {
    "agent": {
        READ_ONLY_AGENT: {
            "mode": "primary",
            "permission": {
                "edit": "deny",
                "bash": "deny",
                "task": "deny",
                "webfetch": "deny",
                "websearch": "deny",
            },
        }
    }
}


def read_only_config(allowed_read_roots: tuple[str, ...]) -> dict[str, Any]:
    config = json.loads(json.dumps(READ_ONLY_CONFIG))
    if allowed_read_roots:
        rules = {"*": "deny"}
        for root in allowed_read_roots:
            rules[root] = "allow"
            rules[f"{root.rstrip('/')}/**" if root != "/" else "/**"] = "allow"
        config["agent"][READ_ONLY_AGENT]["permission"]["external_directory"] = rules
    return config


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
        allowed_read_roots: tuple[str, ...] = (),
        allowed_write_roots: tuple[str, ...] = (),
        timeout_sec: int = 1800,
    ) -> list[str]:
        del timeout_sec
        del allowed_write_roots
        read_only = not writes_allowed and not runs_commands
        command = ["opencode"]
        if read_only:
            config = read_only_config(allowed_read_roots)
            config_content = json.dumps(config, separators=(",", ":"))
            command = [
                "env",
                f"OPENCODE_CONFIG_CONTENT={config_content}",
                "opencode",
            ]
        command.extend(
            [
                "run",
                "--format",
                "json",
                "--dir",
                str(project_root),
                "-m",
                model,
                "--variant",
                effort,
                *(["--agent", READ_ONLY_AGENT] if read_only else []),
                prompt_path.read_text(encoding="utf-8"),
            ]
        )
        return command

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
            tool = part.get("tool")
            inputs = state.get("input") if isinstance(state, dict) else None
            path = None
            if (
                status == "completed"
                and tool in {"write", "edit", "patch", "apply_patch"}
                and isinstance(inputs, dict)
            ):
                candidate = inputs.get("filePath") or inputs.get("file_path") or inputs.get("path")
                path = candidate if isinstance(candidate, str) else None
            return [
                NormalizedEvent(
                    kind="item_started" if status in UNFINISHED else "item_completed",
                    item_id=part.get("id") if isinstance(part.get("id"), str) else None,
                    item_type=part.get("tool") if isinstance(part.get("tool"), str) else "tool",
                    changed_paths=(path,) if path else (),
                    succeeded=status == "completed" if status not in UNFINISHED else None,
                )
            ]
        return []

    def parse_stderr_lines(self, raw_line: str) -> list[NormalizedEvent]:
        return []
