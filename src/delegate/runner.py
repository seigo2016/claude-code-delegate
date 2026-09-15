"""The detached process that supervises one worker run.

How a run ended is decided from what was observed, not from the exit code alone.
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

from delegate import diagnose, envelope, events, liveness, store, workspace
from delegate.adapters import registry
from delegate.adapters.base import WorkerAdapter


class _Tail:
    """Reads a growing file forward only, in bytes so the offset means one thing."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.partial = b""

    def lines(self) -> list[str]:
        try:
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                chunk = handle.read()
                self.offset = handle.tell()
        except OSError:
            return []
        *complete, self.partial = (self.partial + chunk).split(b"\n")
        return [line.decode("utf-8", "replace") for line in complete if line]


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
        store.write_json(recovered, envelope.from_text(view.last_agent_message) or {})
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

    try:
        _supervise(task_dir, state, adapter)
    except liveness.AlreadyHeld:
        # This process writes nothing to a terminal, so the refusal has to land
        # in the task's own log or it is lost.
        store.append_jsonl(
            task_dir / "events.jsonl",
            {"ts": events.now_iso(), "task_id": state["task_id"], "refused": "already_supervised"},
        )
        return 1
    return 0


def _supervise(task_dir: Path, state: dict[str, Any], adapter: WorkerAdapter) -> None:
    with liveness.hold(Path(state["lock_path"])):
        # A cancellation can land between the handover and this lock. Reading the
        # state again here is the only way to see one that arrived in that gap.
        state = store.read_json(task_dir / "state.json")
        if state["status"] == "cancellation_requested":
            events.emit(task_dir, "cancelled", terminal_reason="cancelled_before_start")
            return

        state = events.emit(task_dir, "running", started_at=events.now_iso())
        project_root = Path(state["project_root"])
        before = workspace.changed_paths(project_root)

        # Written per task, so it is right however this package was installed.
        schema_path = task_dir / "result.schema.json"
        store.write_json(schema_path, envelope.json_schema())

        command = adapter.build_command(
            project_root=Path(state["project_root"]),
            prompt_path=Path(state["prompt_path"]),
            result_path=Path(state["result_path"]),
            schema_path=schema_path,
            model=str(state["model"]),
            effort=str(state["effort"]),
            writes_allowed=bool(state["writes_allowed"]),
            runs_commands=bool(state["runs_commands"]),
            allowed_read_roots=tuple(state["allowed_read_roots"]),
            timeout_sec=int(state.get("timeout_sec", 1800)),
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
            try:
                process = subprocess.Popen(
                    command,
                    stdin=prompt,
                    stdout=event_sink,
                    stderr=error_sink,
                    start_new_session=True,
                )
            except OSError as error:
                events.emit(
                    task_dir,
                    "failed",
                    terminal_reason="launch_error",
                    launch_error=f"{type(error).__name__}: {error}",
                    duration_sec=round(time.monotonic() - started, 3),
                )
                return
            event_tail = _Tail(events_path)
            stderr_tail = _Tail(stderr_path)
            heartbeat = max(float(os.environ.get("DELEGATE_HEARTBEAT_SEC", "5")), 0.01)
            deadline = started + float(state["timeout_sec"])
            next_beat = time.monotonic()

            while process.poll() is None:
                now = time.monotonic()
                if cancelled or _cancellation_recorded(task_dir):
                    cancelled = True
                    _terminate(process)
                    break
                if now >= deadline:
                    timed_out = True
                    _terminate(process)
                    break
                if now >= next_beat:
                    view = _absorb(view, adapter, event_tail, stderr_tail)
                    events.update(task_dir, **_snapshot(view))
                    next_beat = now + heartbeat
                time.sleep(min(0.05, heartbeat))
            exit_code = process.wait()

        view = _absorb(view, adapter, event_tail, stderr_tail)
        fields: dict[str, Any] = {
            "exit_code": exit_code,
            "duration_sec": round(time.monotonic() - started, 3),
            **_snapshot(view),
        }
        fields.update(_write_scope(project_root, before, task_dir, view))
        _finish(task_dir, state, view, fields, cancelled=cancelled, timed_out=timed_out)


def _write_scope(
    project_root: Path,
    before: set[str] | None,
    task_dir: Path,
    view: diagnose.RunView,
) -> dict[str, Any]:
    allowed = store.read_json(task_dir / "packet.json")["allowed_writes"]
    after = workspace.changed_paths(project_root) if before is not None else None
    if before is None or after is None:
        return {
            "write_scope_checked": False,
            "unauthorized_writes": [],
            "unattributed_workspace_changes": [],
        }
    worker_changes = workspace.relative_paths(project_root, view.changed_paths)
    workspace_changes = after - before
    unattributed = workspace_changes - worker_changes
    return {
        "write_scope_checked": not unattributed,
        "unauthorized_writes": workspace.unauthorized(set(), worker_changes, allowed),
        "unattributed_workspace_changes": sorted(unattributed),
    }


def _cancellation_recorded(task_dir: Path) -> bool:
    """A signal can be lost or arrive before there is anything to kill; the
    record of the cancellation cannot."""
    try:
        return store.read_json(task_dir / "state.json")["status"] == "cancellation_requested"
    except (OSError, ValueError, KeyError):
        return False


def _absorb(
    view: diagnose.RunView, adapter: WorkerAdapter, event_tail: _Tail, stderr_tail: _Tail
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
        return envelope.from_text(view.last_agent_message) or {}, "final_message"
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
    failure = diagnose.classify_failure(view)
    failure_field = {"failure_class": failure} if failure is not None else {}

    if fields["exit_code"] != 0:
        events.emit(
            task_dir,
            "failed",
            terminal_reason="nonzero_exit",
            **failure_field,
            **_keep_last_message(task_dir, view),
            **fields,
        )
        return

    if fields.get("unauthorized_writes"):
        events.emit(
            task_dir,
            "failed",
            terminal_reason="write_scope_violation",
            **failure_field,
            **fields,
        )
        return

    result, source = _result_of(state, view)
    if result is None:
        events.emit(
            task_dir,
            "failed",
            terminal_reason="empty_result",
            **failure_field,
            **_keep_last_message(task_dir, view),
            **fields,
        )
        return
    if source == "final_message":
        # So that a reader of the task finds the result in one place either way.
        store.write_json(Path(state["result_path"]), result)
    problems = envelope.violations(result)
    if problems:
        events.emit(
            task_dir,
            "failed",
            terminal_reason="invalid_result",
            result_problems=problems,
            **failure_field,
            **_keep_last_message(task_dir, view),
            **fields,
        )
        return
    terminal = "decision_needed" if result["status"] == "decision_needed" else "completed"
    events.emit(task_dir, terminal, terminal_reason="normal_exit", **fields)
