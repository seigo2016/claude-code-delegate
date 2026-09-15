"""Antigravity CLI (agy), mapped onto the normalized event kinds."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from delegate.adapters.base import NormalizedEvent


class AgyAdapter:
    name = "agy"

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
    ) -> list[str]:
        del prompt_path
        del result_path
        del allowed_read_roots

        command = [
            "agy",
            "--input-format",
            "text",
            "--output-format",
            "stream-json",
            "--dangerously-skip-permissions",
            "--disable-slash-commands",
            "--add-dir",
            str(project_root),
        ]

        if model:
            command.extend(["--model", model])
        if effort:
            command.extend(["--effort", effort])

        if not writes_allowed and not runs_commands:
            command.extend(["--mode", "plan"])
        else:
            command.extend(["--mode", "accept-edits"])

        if schema_path.exists():
            command.extend(["--json-schema", str(schema_path)])

        return command

    def parse_events(self, raw_line: str) -> list[NormalizedEvent]:
        try:
            event: Any = json.loads(raw_line)
        except json.JSONDecodeError:
            return []
        if not isinstance(event, dict):
            return []

        event_type = event.get("event")
        if event_type == "init":
            session_id = event.get("conversation_id")
            return [
                NormalizedEvent(
                    kind="session_started",
                    session_id=session_id if isinstance(session_id, str) else None,
                )
            ]

        if event_type == "result":
            result_obj = event.get("result")
            text = None
            succeeded = None
            if isinstance(result_obj, dict):
                text = result_obj.get("response")
                succeeded = result_obj.get("status") == "SUCCESS"
            events = [
                NormalizedEvent(
                    kind="turn_completed",
                    text=text if isinstance(text, str) else None,
                    succeeded=succeeded,
                )
            ]
            if isinstance(text, str):
                events.insert(
                    0,
                    NormalizedEvent(kind="item_completed", item_type="agent_message", text=text),
                )
            return events

        if event_type == "step_update":
            step = event.get("step_update")
            if not isinstance(step, dict):
                return []
            state = step.get("state")
            step_type = step.get("step_type")
            step_index = str(step.get("step_index", ""))

            if state not in {"ACTIVE", "DONE"}:
                return []

            kind = "item_started" if state == "ACTIVE" else "item_completed"
            changed_paths: tuple[str, ...] = ()
            text = None
            item_type = step_type if isinstance(step_type, str) else None

            if step_type == "tool":
                tool_name = step.get("tool_name", "")
                if tool_name == "run_command":
                    item_type = "command_execution"
                elif tool_name == "search_web":
                    item_type = "web_search"
                else:
                    item_type = "dynamic_tool_call"

                tool_info = step.get("tool_info")
                if isinstance(tool_info, dict):
                    params = tool_info.get("parameters")
                    if isinstance(params, dict):
                        target = (
                            params.get("TargetFile")
                            or params.get("target_file")
                            or params.get("AbsolutePath")
                            or params.get("file_path")
                            or params.get("filePath")
                            or params.get("path")
                        )
                        if isinstance(target, str):
                            changed_paths = (target,)
                text = tool_name
            elif step_type == "agent_response":
                text = step.get("text_delta")

            return [
                NormalizedEvent(
                    kind=kind,
                    item_id=step_index,
                    item_type=item_type,
                    text=text if isinstance(text, str) else None,
                    changed_paths=changed_paths,
                )
            ]

        return []

    def parse_stderr_lines(self, raw_line: str) -> list[NormalizedEvent]:
        del raw_line
        return []
