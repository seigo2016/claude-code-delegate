"""The detached process that supervises one worker run.

It holds the task's lock for its whole life, so anyone can tell whether it is
still there. It reads the worker's output forward only, never re-reading what it
has already seen, and it decides how the run ended from what it observed rather
than from the exit code alone.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from delegate import diagnose, envelope, events, liveness, store
from delegate.adapters import registry


class _Tail:
    """Reads a growing file forward only."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0

    def lines(self) -> list[str]:
        try:
            with self.path.open(encoding="utf-8", errors="replace") as handle:
                handle.seek(self.offset)
                text = handle.read()
                self.offset = handle.tell()
        except OSError:
            return []
        if not text.endswith("\n") and text:
            # Keep the partial last line for the next read.
            text, _, remainder = text.rpartition("\n")
            self.offset -= len(remainder.encode("utf-8"))
        return [line for line in text.splitlines() if line]


def _snapshot(view: diagnose.RunView) -> dict[str, Any]:
    return {
        "event_count": view.event_count,
        "session_id": view.session_id,
        "turn_completed": view.turn_completed,
        "runtime_warnings": view.runtime_warnings,
        "heartbeat_at": events.now_iso(),
    }


def _keep_last_message(task_dir: Path, view: diagnose.RunView) -> dict[str, Any]:
    if view.last_agent_message is None:
        return {"recovery_status": "none"}
    message_path = task_dir / "last-agent-message.txt"
    message_path.write_text(view.last_agent_message, encoding="utf-8")
    fields: dict[str, Any] = {
        "recovery_status": "raw",
        "last_agent_message_path": str(message_path),
    }
    if diagnose.has_usable_result(view):
        recovered = task_dir / "recovered-result.json"
        store.write_json(recovered, json.loads(view.last_agent_message))
        fields["recovery_status"] = "usable"
        fields["recovered_result_path"] = str(recovered)
    return fields


def _terminate(process: subprocess.Popen[bytes], grace: float = 2.0) -> None:
    if process.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    deadline = time.monotonic() + grace
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if process.poll() is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)


def run(task_dir: Path) -> int:
    state = store.read_json(task_dir / "state.json")
    adapter = registry.get(str(state["adapter"]))

    with liveness.hold(Path(state["lock_path"])):
        if state["status"] == "cancellation_requested":
            events.emit(task_dir, "cancelled", terminal_reason="cancelled_before_start")
            return 0

        # The contract travels with the task rather than being looked up on disk,
        # so it is correct however this package was installed, and it stays
        # readable next to the run it governed.
        schema_path = task_dir / "result.schema.json"
        store.write_json(schema_path, envelope.json_schema())

        command = adapter.build_command(
            project_root=Path(state["project_root"]),
            prompt_path=Path(state["prompt_path"]),
            result_path=Path(state["result_path"]),
            schema_path=schema_path,
            model=str(state["model"]),
            effort=str(state["effort"]),
        )

        cancelled = False
        process: subprocess.Popen[bytes] | None = None

        def on_signal(_signum: int, _frame: Any) -> None:
            nonlocal cancelled
            cancelled = True
            if process is not None:
                _terminate(process)

        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)

        events_path = Path(state["events_path"])
        stderr_path = Path(state["stderr_path"])
        view = diagnose.RunView()
        started = time.monotonic()
        timed_out = False

        with (
            Path(state["prompt_path"]).open("rb") as prompt,
            events_path.open("wb") as event_sink,
            stderr_path.open("wb") as error_sink,
        ):
            process = subprocess.Popen(
                command,
                stdin=prompt,
                stdout=event_sink,
                stderr=error_sink,
                start_new_session=True,
            )
            event_tail = _Tail(events_path)
            stderr_tail = _Tail(stderr_path)
            heartbeat = max(float(os.environ.get("DELEGATE_HEARTBEAT_SEC", "5")), 0.01)
            deadline = started + float(state["timeout_sec"])
            next_beat = time.monotonic()

            while process.poll() is None:
                now = time.monotonic()
                if now >= deadline:
                    timed_out = True
                    _terminate(process)
                    break
                if now >= next_beat:
                    view = _absorb(view, adapter, event_tail, stderr_tail)
                    events.emit(task_dir, str(state["status"]), **_snapshot(view))
                    next_beat = now + heartbeat
                time.sleep(min(0.05, heartbeat))
            exit_code = process.wait()

        view = _absorb(view, adapter, event_tail, stderr_tail)
        fields: dict[str, Any] = {
            "exit_code": exit_code,
            "duration_sec": round(time.monotonic() - started, 3),
            **_snapshot(view),
        }
        _finish(task_dir, state, view, fields, cancelled=cancelled, timed_out=timed_out)
    return 0


def _absorb(
    view: diagnose.RunView, adapter: Any, event_tail: _Tail, stderr_tail: _Tail
) -> diagnose.RunView:
    for line in event_tail.lines():
        for event in adapter.parse_events(line):
            view = diagnose.observe(view, event)
    for line in stderr_tail.lines():
        for event in adapter.parse_stderr_lines(line):
            view = diagnose.observe(view, event)
    return view


def _result_of(state: dict[str, Any], view: diagnose.RunView) -> tuple[dict[str, Any] | None, str]:
    """The result the worker produced, from a file if it can write one, else from
    its last message. ``None`` means it produced nothing to judge."""
    result_path = Path(state["result_path"])
    if result_path.exists() and result_path.stat().st_size:
        try:
            return store.read_json(result_path), "file"
        except (OSError, ValueError, json.JSONDecodeError):
            return {}, "file"
    if view.last_agent_message is not None:
        try:
            parsed = json.loads(view.last_agent_message)
        except json.JSONDecodeError:
            return {}, "final_message"
        return (parsed if isinstance(parsed, dict) else {}), "final_message"
    return None, "none"


def _finish(
    task_dir: Path,
    state: dict[str, Any],
    view: diagnose.RunView,
    fields: dict[str, Any],
    *,
    cancelled: bool,
    timed_out: bool,
) -> None:
    current = store.read_json(task_dir / "state.json")
    if cancelled or current.get("status") == "cancellation_requested":
        events.emit(task_dir, "cancelled", terminal_reason="cancelled", **fields)
        return
    if timed_out:
        events.emit(
            task_dir,
            "timeout",
            terminal_reason="timeout",
            failure_class=diagnose.classify_timeout(view),
            **_keep_last_message(task_dir, view),
            **fields,
        )
        return
    if fields["exit_code"] != 0:
        events.emit(
            task_dir,
            "failed",
            terminal_reason="nonzero_exit",
            **_keep_last_message(task_dir, view),
            **fields,
        )
        return

    result, source = _result_of(state, view)
    if result is None:
        events.emit(
            task_dir,
            "failed",
            terminal_reason="empty_result",
            **_keep_last_message(task_dir, view),
            **fields,
        )
        return
    if source == "final_message":
        # Only one backend can be handed an output path; the others answer in
        # their last message. Recording it here means the rest of the system,
        # and anyone reading the task afterwards, sees one kind of result.
        store.write_json(Path(state["result_path"]), result)
    problems = envelope.violations(result)
    if problems:
        events.emit(
            task_dir,
            "failed",
            terminal_reason="invalid_result",
            result_problems=problems,
            **_keep_last_message(task_dir, view),
            **fields,
        )
        return
    terminal = "decision_needed" if result["status"] == "decision_needed" else "completed"
    events.emit(task_dir, terminal, terminal_reason="normal_exit", **fields)
