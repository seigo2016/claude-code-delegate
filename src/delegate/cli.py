"""The `delegate` command."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from delegate import config, events, liveness, runner, store, tasks

WATCH_POLL_SEC = 1.0
# How long a supervisor may take to claim its lock before we believe it is gone.
STARTUP_GRACE_SEC = 30.0


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _fail(message: str, code: int = 2, **detail: Any) -> int:
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
            effort=args.effort,
            timeout=args.timeout,
            fresh=args.fresh,
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
        return _fail("task_not_terminal", 4, status=state["status"])
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


def cmd_reconcile(args: argparse.Namespace) -> int:
    project_root = _root(args.project_root)
    reconciled = []
    for state in _states(project_root):
        task_dir = Path(state["task_dir"])
        if (
            state["status"] in events.TERMINAL_STATES
            or liveness.worker_alive(Path(state["lock_path"]))
            or _still_starting(state)
        ):
            reconciled.append(state)
            continue
        result_path = Path(state["result_path"])
        if result_path.exists() and result_path.stat().st_size:
            reconciled.append(
                events.emit(task_dir, "degraded", terminal_reason="worker_gone_with_result")
            )
        else:
            reconciled.append(events.emit(task_dir, "orphaned", terminal_reason="worker_gone"))
    _print({"tasks": [_line(state) for state in reconciled]})
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Wait until something is worth waking the session for.

    Exit 0 means there is a finished, uncollected task. Any other exit means the
    session should be left alone.
    """
    project_root = _root(args.project_root)
    try:
        with liveness.hold(tasks.task_root(project_root) / "watch.lock"):
            return _wait_for_something_to_collect(project_root)
    except liveness.AlreadyHeld:
        # A watcher starts after every Bash call; without this they would pile
        # up and each wake the session about the same finished task.
        _print({"ready": []})
        return 1


def _wait_for_something_to_collect(project_root: Path) -> int:
    limit = float(os.environ.get("DELEGATE_WATCH_SEC", "3600"))
    deadline = time.monotonic() + limit
    while True:
        states = _states(project_root)
        ready = [
            state
            for state in states
            if state["status"] in events.TERMINAL_STATES and not state.get("delivered_at")
        ]
        if ready:
            _print({"ready": [_line(state) for state in ready]})
            return 0
        waiting = any(state["status"] in events.ACTIVE_STATES for state in states)
        if not waiting or time.monotonic() >= deadline:
            _print({"ready": []})
            return 1
        time.sleep(WATCH_POLL_SEC)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="delegate", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def with_root(target: argparse.ArgumentParser) -> argparse.ArgumentParser:
        target.add_argument("--project-root", default=None)
        return target

    submit = with_root(sub.add_parser("submit", help="start a detached worker"))
    submit.add_argument("--role", required=True)
    submit.add_argument("--title", required=True)
    submit.add_argument("--packet", required=True, help="path to the task packet JSON")
    submit.add_argument("--worker", default=None)
    submit.add_argument("--effort", default=None)
    submit.add_argument("--timeout", type=int, default=None)
    submit.add_argument("--fresh", action="store_true")
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

    run = sub.add_parser("_run", help=argparse.SUPPRESS)
    run.add_argument("task_dir")
    run.set_defaults(func=lambda args: runner.run(Path(args.task_dir)))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
