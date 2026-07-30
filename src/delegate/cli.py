"""The `delegate` command."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn

from delegate import config, events, liveness, runner, store, tasks

WATCH_POLL_SEC = 1.0
# How long a supervisor may take to claim its lock before we believe it is gone.
STARTUP_GRACE_SEC = 30.0


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _fail(message: str, code: int = 1, **detail: Any) -> int:
    _print({"error": message, **detail})
    return code


def _root(value: str | None) -> Path:
    return Path(value).expanduser().resolve() if value else Path.cwd()


def _load_task(project_root: Path, task_id: str) -> tuple[Path, dict[str, Any]]:
    task_dir = tasks.task_root(project_root) / task_id
    return task_dir, store.read_json(task_dir / "state.json")


def cmd_submit(args: argparse.Namespace) -> int:
    project_root = _root(args.project_root)
    try:
        settings = config.load(tasks.settings_path(project_root))
    except OSError:
        return _fail(f"no settings at {tasks.settings_path(project_root)}")
    try:
        value = json.loads(Path(args.packet).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return _fail(f"cannot read packet: {error}")

    try:
        handle = tasks.submit(
            project_root=project_root,
            settings=settings,
            role=args.role,
            title=args.title,
            value=value,
            worker=args.worker,
        )
    except tasks.SubmitRefused as refused:
        return _fail(str(refused), **refused.detail)
    except (config.ConfigError, ValueError) as error:
        return _fail(str(error))
    _print(handle)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    try:
        _, state = _load_task(_root(args.project_root), args.task_id)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return _fail(f"task_not_found: {error}")
    _print(state)
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    project_root = _root(args.project_root)
    try:
        task_dir, state = _load_task(project_root, args.task_id)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return _fail(f"task_not_found: {error}")
    if state["status"] not in events.TERMINAL_STATES:
        return _fail(
            "task_not_terminal",
            4,
            status=state["status"],
            next_action="end_turn",
            completion_delivery="async_rewake",
        )
    if state.get("delivered_at"):
        return _fail("already_delivered", 3, delivered_at=state["delivered_at"])

    result: Any = None
    if state["status"] in {"completed", "decision_needed"}:
        result = store.read_json(Path(state["result_path"]))
    _print(
        {
            "task_id": state["task_id"],
            "status": state["status"],
            "role": state["role"],
            "worker": state["worker"],
            "model": state["model"],
            "effort": state["effort"],
            "duration_sec": state.get("duration_sec"),
            "session_id": state.get("session_id"),
            "terminal_reason": state.get("terminal_reason"),
            "failure_class": state.get("failure_class"),
            "recovery_status": state.get("recovery_status"),
            "result": result,
            "artifacts": {
                "packet": state["packet_path"],
                "prompt": state["prompt_path"],
                "result": state["result_path"],
                "events": state["events_path"],
                "stderr": state["stderr_path"],
                "state": state["state_path"],
                "last_agent_message": state.get("last_agent_message_path"),
                "recovered_result": state.get("recovered_result_path"),
            },
        }
    )
    events.emit(task_dir, state["status"], delivered_at=events.now_iso())
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    try:
        task_dir, state = _load_task(_root(args.project_root), args.task_id)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return _fail(f"task_not_found: {error}")
    if state["status"] in events.TERMINAL_STATES:
        _print(state)
        return 0
    # Recording the request is enough: the supervisor reads it within a poll and
    # stops the worker itself. Signalling it here could instead kill it outright,
    # before it has installed a handler, leaving nobody to record the outcome.
    _print(events.emit(task_dir, "cancellation_requested"))
    return 0


def _line(state: dict[str, Any]) -> dict[str, Any]:
    """One task, short enough to read at a glance in a session start."""
    return {
        "task_id": state["task_id"],
        "project_root": state["project_root"],
        "title": state["title"],
        "role": state["role"],
        "status": state["status"],
        "terminal_reason": state.get("terminal_reason"),
        "collected": bool(state.get("delivered_at")),
    }


def _states(project_root: Path) -> list[dict[str, Any]]:
    found = []
    for projection in sorted(tasks.task_root(project_root).glob("*/state.json")):
        try:
            found.append(store.read_json(projection))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return found


def _still_starting(state: dict[str, Any]) -> bool:
    if state["status"] != "starting":
        return False
    created = datetime.fromisoformat(str(state["created_at"]))
    return (datetime.now().astimezone() - created).total_seconds() < STARTUP_GRACE_SEC


def _reconcile_state(state: dict[str, Any]) -> dict[str, Any]:
    task_dir = Path(state["task_dir"])
    if (
        state["status"] in events.TERMINAL_STATES
        or liveness.worker_alive(Path(state["lock_path"]))
        or _still_starting(state)
    ):
        return state
    result_path = Path(state["result_path"])
    if result_path.exists() and result_path.stat().st_size:
        return events.emit(task_dir, "degraded", terminal_reason="worker_gone_with_result")
    return events.emit(task_dir, "orphaned", terminal_reason="worker_gone")


def cmd_reconcile(args: argparse.Namespace) -> int:
    project_root = _root(args.project_root)
    reconciled = [_reconcile_state(state) for state in _states(project_root)]
    _print({"tasks": [_line(state) for state in reconciled]})
    return 0


#: An ``asyncRewake`` hook wakes the session on exit code 2 and on no other code,
#: and the woken session is handed the hook's output in place of a task list. So 2
#: is reserved, here and in the launcher, for a task being ready.
WAKE = 2
QUIET = 0


def _handle_from_stdout(stdout: str) -> dict[str, Any]:
    handles = []
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(value, dict)
            and value.get("completion_delivery") == "async_rewake"
            and isinstance(value.get("project_root"), str)
            and isinstance(value.get("task_id"), str)
        ):
            handles.append(value)
    if len(handles) != 1:
        raise ValueError("expected one delegate handle")
    return handles[0]


def cmd_watch(args: argparse.Namespace) -> int:
    """Wait until something is worth waking the session for.

    Exit ``WAKE`` means there is a finished, uncollected task. Exit ``QUIET`` means
    the session should be left alone.
    """
    return _watch(_root(args.project_root))


def cmd_hook_watch(args: argparse.Namespace) -> int:
    """Watch only when the Bash result is a handle returned by ``delegate submit``."""
    try:
        hook_input = json.load(sys.stdin)
        stdout = hook_input["tool_response"]["stdout"]
        handle = _handle_from_stdout(stdout)
        project_root = _root(handle["project_root"])
        _, state = _load_task(project_root, handle["task_id"])
    except (KeyError, TypeError, OSError, ValueError, json.JSONDecodeError):
        _print({"ready": []})
        return QUIET
    if (
        hook_input.get("hook_event_name") != "PostToolUse"
        or hook_input.get("tool_name") != "Bash"
        or handle.get("completion_delivery") != "async_rewake"
        or state.get("project_root") != str(project_root)
        or state.get("task_id") != handle.get("task_id")
    ):
        _print({"ready": []})
        return QUIET
    return _watch_task(project_root, state["task_id"])


def _watch(project_root: Path) -> int:
    try:
        with liveness.hold(tasks.task_root(project_root) / "watch.lock"):
            return _wait_for_something_to_collect(project_root)
    except liveness.AlreadyHeld:
        # Deduplicated submissions can return the same active task handle.
        _print({"ready": []})
        return QUIET


def _watch_task(project_root: Path, task_id: str) -> int:
    task_dir = tasks.task_root(project_root) / task_id
    try:
        with liveness.hold(task_dir / "watch.lock"):
            return _wait_for_task(project_root, task_id)
    except liveness.AlreadyHeld:
        _print({"ready": []})
        return QUIET


def _wait_for_task(project_root: Path, task_id: str) -> int:
    limit = float(os.environ.get("DELEGATE_WATCH_SEC", "3600"))
    deadline = time.monotonic() + limit
    while True:
        try:
            _, state = _load_task(project_root, task_id)
        except (OSError, ValueError, json.JSONDecodeError):
            _print({"ready": []})
            return QUIET
        state = _reconcile_state(state)
        if state["status"] in events.TERMINAL_STATES and not state.get("delivered_at"):
            _print({"ready": [_line(state)]})
            return WAKE
        if state["status"] not in events.ACTIVE_STATES or time.monotonic() >= deadline:
            _print({"ready": []})
            return QUIET
        time.sleep(WATCH_POLL_SEC)


def _wait_for_something_to_collect(project_root: Path) -> int:
    limit = float(os.environ.get("DELEGATE_WATCH_SEC", "3600"))
    deadline = time.monotonic() + limit
    while True:
        states = [_reconcile_state(state) for state in _states(project_root)]
        ready = [
            state
            for state in states
            if state["status"] in events.TERMINAL_STATES and not state.get("delivered_at")
        ]
        if ready:
            _print({"ready": [_line(state) for state in ready]})
            return WAKE
        waiting = any(state["status"] in events.ACTIVE_STATES for state in states)
        if not waiting or time.monotonic() >= deadline:
            _print({"ready": []})
            return QUIET
        time.sleep(WATCH_POLL_SEC)


class _Parser(argparse.ArgumentParser):
    """An argument parser that does not exit 2, since argparse's 2 means ``WAKE`` here.

    A hook written by one version of this plugin can outlive the CLI it calls.
    """

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(f"delegate: {message}", file=sys.stderr)
        raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="delegate", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def with_root(target: argparse.ArgumentParser) -> argparse.ArgumentParser:
        target.add_argument("--project-root", default=None)
        return target

    submit = with_root(sub.add_parser("submit", help="start a detached worker"))
    submit.add_argument("--role", required=True)
    submit.add_argument("--title", required=True)
    submit.add_argument("--packet", required=True, help="path to the task packet JSON")
    submit.add_argument("--worker", default=None)
    submit.set_defaults(func=cmd_submit)

    status = with_root(sub.add_parser("status", help="read one task's state"))
    status.add_argument("task_id")
    status.set_defaults(func=cmd_status)

    collect = with_root(sub.add_parser("collect", help="take delivery of a finished task"))
    collect.add_argument("task_id")
    collect.set_defaults(func=cmd_collect)

    cancel = with_root(sub.add_parser("cancel", help="stop a task"))
    cancel.add_argument("task_id")
    cancel.set_defaults(func=cmd_cancel)

    reconcile = with_root(sub.add_parser("reconcile", help="classify tasks whose worker is gone"))
    reconcile.set_defaults(func=cmd_reconcile)

    watch = with_root(sub.add_parser("watch", help="wait until a task is worth collecting"))
    watch.set_defaults(func=cmd_watch)

    hook_watch = sub.add_parser("_hook-watch", help=argparse.SUPPRESS)
    hook_watch.set_defaults(func=cmd_hook_watch)

    run = sub.add_parser("_run", help=argparse.SUPPRESS)
    run.add_argument("task_dir")
    run.set_defaults(func=lambda args: runner.run(Path(args.task_dir)))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
