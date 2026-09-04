"""Codex CLI, mapped onto the normalized event kinds."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from delegate.adapters.base import NormalizedEvent


def cache_root() -> Path:
    """Where build tools keep the cache they insist on writing to.

    `workspace-write` covers the repository and the temporary directory, but not the
    home directory, and `uv run pytest` was measured stopping before pytest started
    because it could not open its cache there.
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")


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
        writes_allowed: bool,
        runs_commands: bool,
        allowed_read_roots: tuple[str, ...] = (),
    ) -> list[str]:
        del allowed_read_roots
        # What keeps a role that runs commands off the repository is the scope check
        # afterwards, not the sandbox.
        writable = writes_allowed or runs_commands
        roots = json.dumps([str(cache_root())]) if writable else "[]"
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
            # Left to ~/.codex/config.toml, how far a worker can reach is decided by
            # a file this project does not own, and differs from machine to machine.
            "--sandbox",
            "workspace-write" if writable else "read-only",
            "-c",
            'approval_policy="never"',
            "-c",
            f"sandbox_workspace_write.network_access={str(writable).lower()}",
            "-c",
            f"sandbox_workspace_write.writable_roots={roots}",
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
            changes = item.get("changes")
            changed_paths = (
                tuple(
                    change["path"]
                    for change in changes
                    if isinstance(change, dict) and isinstance(change.get("path"), str)
                )
                if isinstance(changes, list)
                else ()
            )
            return [
                NormalizedEvent(
                    kind="item_started" if kind == "item.started" else "item_completed",
                    item_id=item.get("id") if isinstance(item.get("id"), str) else None,
                    item_type=item.get("type") if isinstance(item.get("type"), str) else None,
                    text=text if isinstance(text, str) else None,
                    changed_paths=changed_paths,
                )
            ]
        return []

    def parse_stderr_lines(self, raw_line: str) -> list[NormalizedEvent]:
        if MODEL_REFRESH_TIMEOUT in raw_line:
            return [NormalizedEvent(kind="runtime_warning", text="model_refresh_timeout")]
        return []
