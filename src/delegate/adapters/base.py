"""What every worker backend must look like from the broker's side.

Stall diagnosis reads only these kinds, so adding a backend cannot change how
failures are classified. One line of output can mean two things, hence the list.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

KINDS = (
    "session_started",
    "item_started",
    "item_completed",
    "turn_completed",
    "runtime_warning",
    "api_error",
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
    usage_source: str | None = None
    usage_tokens: tuple[tuple[str, int], ...] = ()
    failure_class: str | None = None


def api_error_event(event: dict[str, Any]) -> NormalizedEvent | None:
    """Normalize a provider error without exposing its unbounded response body."""
    if event.get("type") != "error":
        return None
    error = event.get("error")
    error = error if isinstance(error, dict) else {}
    data = error.get("data")
    data = data if isinstance(data, dict) else {}
    response_error: dict[str, Any] = {}
    response_body = data.get("responseBody")
    if isinstance(response_body, str):
        try:
            parsed = json.loads(response_body)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
            response_error = parsed["error"]

    status_codes = (data.get("statusCode"), error.get("statusCode"), event.get("statusCode"))
    status_is_auth = any(
        isinstance(status_code, int)
        and not isinstance(status_code, bool)
        and status_code in {401, 403}
        for status_code in status_codes
    )
    error_types = (response_error.get("type"), error.get("type"), data.get("type"))
    codes = (response_error.get("code"), error.get("code"), data.get("code"), event.get("code"))
    code_is_auth = any(
        isinstance(code, str) and ("api_key" in code.lower() or "auth" in code.lower())
        for code in codes
    )
    failure_class = (
        "auth_failed"
        if status_is_auth or "authentication_error" in error_types or code_is_auth
        else "worker_api_error"
    )
    message = next(
        (
            value
            for value in (
                response_error.get("message"),
                error.get("message"),
                data.get("message"),
                event.get("message"),
            )
            if isinstance(value, str) and value
        ),
        None,
    )
    return NormalizedEvent(
        kind="api_error",
        text=message[:200] if message is not None else None,
        failure_class=failure_class,
    )


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
        allowed_read_roots: tuple[str, ...] = (),
        allowed_write_roots: tuple[str, ...] = (),
        timeout_sec: int = 1800,
    ) -> list[str]: ...

    def parse_events(self, raw_line: str) -> list[NormalizedEvent]: ...

    def parse_stderr_lines(self, raw_line: str) -> list[NormalizedEvent]: ...
