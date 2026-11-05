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
from typing import Any, cast

from delegate import diagnose, envelope, events, liveness, store, workspace
from delegate.adapters import registry
from delegate.adapters.base import WorkerAdapter
from delegate.adapters.codex import CodexAdapter, cache_root

#: A worker that never wrote a byte never did anything observable, so waiting
#: out its whole deadline only delays the inevitable timeout. It is stopped
#: once silent past this long, or past half its deadline for short tasks.
SILENCE_LIMIT_SEC = 60.0


def _worker_has_spoken(events_path: Path, stderr_path: Path) -> bool:
    """Whether the worker wrote anything at all, on either stream.

    Anything unreadable counts as spoken: silence must be certain to act on.
    """
    try:
        return events_path.stat().st_size > 0 or stderr_path.stat().st_size > 0
    except OSError:
        return True


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
    snapshot = {
        "event_count": view.event_count,
        "session_id": view.session_id,
        "turn_completed": view.turn_completed,
        "runtime_warnings": view.runtime_warnings,
        "heartbeat_at": events.now_iso(),
    }
    if view.usage_tokens:
        snapshot["provider_usage"] = {
            "source": view.usage_source,
            "tokens": dict(view.usage_tokens),
        }
    return snapshot


def _keep_last_message(
    task_dir: Path, view: diagnose.RunView, *, writes_allowed: bool = False
) -> dict[str, Any]:
    if view.last_agent_message is None:
        return {"recovery_status": "none"}
    message_path = task_dir / "last-agent-message.txt"
    message_path.write_text(view.last_agent_message, encoding="utf-8")
    fields: dict[str, Any] = {
        "recovery_status": "raw",
        "last_agent_message_path": str(message_path),
    }
    parsed = envelope.from_text(view.last_agent_message)
    if (
        parsed is not None
        and not envelope.violations(
            envelope.sanitize(parsed, writes_allowed=writes_allowed), writes_allowed=writes_allowed
        )
        and not envelope.incomplete_write_work(parsed, writes_allowed=writes_allowed)
    ):
        recovered = task_dir / "recovered-result.json"
        raw_result = envelope.from_text(view.last_agent_message)
        sanitized = (
            envelope.sanitize(raw_result, writes_allowed=writes_allowed)
            if raw_result is not None
            else {}
        )
        store.write_json(recovered, sanitized)
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
    try:
        adapter = registry.get(str(state["adapter"]))
        _supervise(task_dir, state, adapter)
    except liveness.AlreadyHeld:
        # This process writes nothing to a terminal, so the refusal has to land
        # in the task's own log or it is lost.
        store.append_jsonl(
            task_dir / "events.jsonl",
            {"ts": events.now_iso(), "task_id": state["task_id"], "refused": "already_supervised"},
        )
        return 1
    except Exception as error:
        if store.read_json(task_dir / "state.json").get("status") in events.TERMINAL_STATES:
            return 1
        events.emit(
            task_dir,
            "failed",
            terminal_reason="supervisor_error",
            failure_class=type(error).__name__,
            failure_message=str(error) or type(error).__name__,
        )
        return 1
    return 0


def _supervise(task_dir: Path, state: dict[str, Any], adapter: WorkerAdapter) -> None:
    process: subprocess.Popen[bytes] | None = None

    def cleanup_worker() -> None:
        if process is not None and process.poll() is None:
            _terminate(process)
            process.wait()

    with liveness.hold(Path(state["lock_path"])), contextlib.ExitStack() as cleanup:
        cleanup.callback(cleanup_worker)
        # A cancellation can land between the handover and this lock. Reading the
        # state again here is the only way to see one that arrived in that gap.
        state = store.read_json(task_dir / "state.json")
        if state["status"] == "cancellation_requested":
            events.emit(task_dir, "cancelled", terminal_reason="cancelled_before_start")
            return

        roots_before = workspace.root_entries(state.get("allowed_write_roots", ()))
        temp_dir = None
        if state["writes_allowed"] or state["runs_commands"]:
            temp_root = Path(state["temp_root"]) if state.get("temp_root") else cache_root() / "tmp"
            temp_dir = temp_root / state["task_id"]
            temp_dir.mkdir(parents=True, exist_ok=True)
        state = events.emit(
            task_dir,
            "running",
            started_at=events.now_iso(),
            root_entries_before=roots_before,
            temp_dir=str(temp_dir) if temp_dir is not None else None,
        )
        project_root = Path(state["project_root"])
        before = workspace.changed_paths(project_root)

        # Written per task, so it is right however this package was installed.
        schema_path = task_dir / "result.schema.json"
        store.write_json(
            schema_path, envelope.json_schema(writes_allowed=bool(state["writes_allowed"]))
        )

        command_args: dict[str, Any] = dict(
            project_root=Path(state["project_root"]),
            prompt_path=Path(state["prompt_path"]),
            result_path=Path(state["result_path"]),
            schema_path=schema_path,
            model=str(state["model"]),
            effort=str(state["effort"]),
            writes_allowed=bool(state["writes_allowed"]),
            runs_commands=bool(state["runs_commands"]),
            allowed_read_roots=tuple(state["allowed_read_roots"]),
            allowed_write_roots=tuple(state.get("allowed_write_roots", ())),
            timeout_sec=int(state.get("timeout_sec", 1800)),
        )
        if state["adapter"] == "codex" and state.get("fast", False):
            command_args["fast"] = True
        if state["adapter"] == "codex" and "isolate_home" in state:
            command_args["isolate_home"] = state["isolate_home"]
        if state["adapter"] == "codex" and temp_dir is not None:
            command_args["temp_dir"] = temp_dir

        command = adapter.build_command(**command_args)
        worker_env = os.environ.copy()
        if temp_dir:
            worker_env["TMPDIR"] = str(temp_dir)
        if state["runs_commands"] and temp_dir:
            worker_env["PYTHONPYCACHEPREFIX"] = str(temp_dir / "pycache")
            worker_env["RUFF_CACHE_DIR"] = str(temp_dir / "ruff-cache")
            worker_env["PYTEST_ADDOPTS"] = (
                worker_env.get("PYTEST_ADDOPTS", "") + " -p no:cacheprovider"
            ).strip()

        cancelled = False

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

        prompt_path = Path(state["prompt_path"])
        resume_count = 0
        resume_results: list[dict[str, Any]] = []
        previous_blockers: list[str] | None = None
        original_session_id: str | None = None
        # Each attempt appends to the task's streams; the deadline covers the whole task.
        while True:
            attempt_started = time.monotonic()
            with (
                prompt_path.open("rb") as prompt,
                events_path.open("ab") as event_sink,
                stderr_path.open("ab") as error_sink,
            ):
                event_tail = _Tail(events_path)
                event_tail.offset = events_path.stat().st_size
                stderr_tail = _Tail(stderr_path)
                stderr_tail.offset = stderr_path.stat().st_size
                try:
                    process = subprocess.Popen(
                        command,
                        stdin=prompt,
                        stdout=event_sink,
                        stderr=error_sink,
                        start_new_session=True,
                        env=worker_env,
                    )
                except OSError as error:
                    if resume_count:
                        resume_results[-1].update(outcome="launch_error")
                        events.update(task_dir, resume_results=resume_results)
                    events.emit(
                        task_dir,
                        "failed",
                        terminal_reason="launch_error",
                        launch_error=f"{type(error).__name__}: {error}",
                        duration_sec=round(time.monotonic() - started, 3),
                    )
                    return
                event_tail_start, stderr_tail_start = event_tail.offset, stderr_tail.offset
                heartbeat = max(float(os.environ.get("DELEGATE_HEARTBEAT_SEC", "5")), 0.01)
                deadline = started + float(state["timeout_sec"])
                silence_limit = min(SILENCE_LIMIT_SEC, float(state["timeout_sec"]) / 2)
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
                    if (
                        now - attempt_started >= silence_limit
                        and events_path.stat().st_size == event_tail_start
                        and stderr_path.stat().st_size == stderr_tail_start
                    ):
                        _terminate(process)
                        timed_out = True
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
            result, _ = _result_of(state, view)
            sanitized = (
                envelope.sanitize(result, writes_allowed=True) if result is not None else None
            )
            valid = sanitized is not None and not envelope.violations(
                sanitized, writes_allowed=True
            )
            incomplete = bool(
                valid and envelope.incomplete_write_work(sanitized, writes_allowed=True)
            )
            if resume_count:
                resume_results[-1].update(
                    outcome=(
                        "cancelled"
                        if cancelled or _cancellation_recorded(task_dir)
                        else "timeout"
                        if timed_out
                        else "nonzero_exit"
                        if exit_code
                        else "write_scope_violation"
                        if fields["unauthorized_writes"]
                        else "incomplete_work"
                        if incomplete
                        else str(sanitized["status"])
                        if valid
                        else "invalid_or_empty_result"
                    ),
                    result=sanitized,
                    exit_code=exit_code,
                )
                events.update(task_dir, resume_results=resume_results)
            blockers = sorted(sanitized["blockers"]) if incomplete else None
            repeated = previous_blockers is not None and blockers == previous_blockers
            if (
                incomplete
                and state["writes_allowed"]
                and state["adapter"] == "codex"
                and view.session_id
                and not repeated
                and resume_count < int(state.get("resume_limit", 2))
                and not cancelled
                and not _cancellation_recorded(task_dir)
                and not timed_out
                and exit_code == 0
                and not fields["unauthorized_writes"]
                and time.monotonic() < deadline
            ):
                original_session_id = original_session_id or view.session_id
                previous_blockers = blockers
                resume_count += 1
                resume_results.append(
                    {"attempt": resume_count, "outcome": "running", "result": None}
                )
                events.update(task_dir, resume_count=resume_count, resume_results=resume_results)
                command = cast(CodexAdapter, adapter).build_resume_command(
                    session_id=original_session_id, **command_args
                )
                prompt_path = task_dir / "resume-prompt.md"
                prompt_path.write_text(
                    "Continue implementing the unfinished work listed in the blockers of your "
                    "previous answer until all implementation is complete. Return only when all "
                    "is complete or a decision is needed. Create temporary files and directories "
                    "only under $TMPDIR; do not create new entries directly under "
                    "allowed_write_roots except packet-named outputs.\n",
                    encoding="utf-8",
                )
                Path(state["result_path"]).unlink(missing_ok=True)
                view = diagnose.RunView(
                    session_id=original_session_id, changed_paths=view.changed_paths
                )
                continue
            _finish(task_dir, state, view, fields, cancelled=cancelled, timed_out=timed_out)
            return


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


def _salvaged_result(
    view: diagnose.RunView, *, writes_allowed: bool = False
) -> dict[str, Any] | None:
    """A result worth delivering despite the timeout, if the worker left one.

    The worker finished its turn but never handed the result back through the
    file it was given. Promoting the message is only honest when it already
    satisfies the contract; anything else stays a timeout.
    """
    parsed = envelope.from_text(view.last_agent_message or "")
    if parsed is None:
        return None
    result = envelope.sanitize(parsed, writes_allowed=writes_allowed)
    if envelope.violations(result, writes_allowed=writes_allowed):
        return None
    if envelope.incomplete_write_work(result, writes_allowed=writes_allowed):
        return None
    return result


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


def _save_result(task_dir: Path, result_path: Path, result: dict[str, Any]) -> None:
    result = dict(result)
    report = result.pop("report", None)
    if isinstance(report, str) and report:
        report_path = task_dir / "report.md"
        report_path.write_text(report, encoding="utf-8")
        result["report_path"] = str(report_path)
    store.write_json(result_path, result)


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
    writes_allowed = bool(state["writes_allowed"])
    if cancelled or current.get("status") == "cancellation_requested":
        events.emit(task_dir, "cancelled", terminal_reason="cancelled", **fields)
        return
    if timed_out:
        salvaged = _salvaged_result(view, writes_allowed=writes_allowed)
        if salvaged is not None:
            _save_result(task_dir, Path(state["result_path"]), salvaged)
            events.emit(
                task_dir,
                "degraded",
                terminal_reason="timeout_with_result",
                failure_class=diagnose.classify_timeout(view),
                **_keep_last_message(task_dir, view, writes_allowed=writes_allowed),
                **fields,
            )
            return
        events.emit(
            task_dir,
            "timeout",
            terminal_reason="timeout",
            failure_class=diagnose.classify_timeout(view),
            **_keep_last_message(task_dir, view, writes_allowed=writes_allowed),
            **fields,
        )
        return
    failure = diagnose.classify_failure(view)
    failure_field = {"failure_class": failure} if failure is not None else {}
    if view.api_failure_message is not None:
        failure_field["failure_message"] = view.api_failure_message

    if fields["exit_code"] != 0:
        events.emit(
            task_dir,
            "failed",
            terminal_reason="nonzero_exit",
            **failure_field,
            **_keep_last_message(task_dir, view, writes_allowed=writes_allowed),
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
            **_keep_last_message(task_dir, view, writes_allowed=writes_allowed),
            **fields,
        )
        return
    result = envelope.sanitize(result, writes_allowed=writes_allowed)
    # So that a reader of the task finds the sanitized result in one place either way.
    problems = envelope.violations(result, writes_allowed=writes_allowed)
    if problems:
        store.write_json(Path(state["result_path"]), result)
        events.emit(
            task_dir,
            "failed",
            terminal_reason="invalid_result",
            result_problems=problems,
            **failure_field,
            **_keep_last_message(task_dir, view, writes_allowed=writes_allowed),
            **fields,
        )
        return
    _save_result(task_dir, Path(state["result_path"]), result)
    if envelope.incomplete_write_work(result, writes_allowed=writes_allowed):
        events.emit(
            task_dir,
            "failed",
            terminal_reason="incomplete_work",
            failure_class="incomplete_work",
            **fields,
        )
        return
    terminal = (
        result["status"] if result["status"] in {"decision_needed", "unverified"} else "completed"
    )
    events.emit(task_dir, terminal, terminal_reason="normal_exit", **fields)
