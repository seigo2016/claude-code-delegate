"""The `delegate` command."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections.abc import Callable, Iterable
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn

from delegate import config, envelope, errors, events, liveness, runner, store, tasks

WATCH_POLL_SEC = 1.0
# How long a supervisor may take to claim its lock before we believe it is gone.
STARTUP_GRACE_SEC = 30.0


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _fail(error: str, reason: str | None = None, **detail: Any) -> int:
    _print({"error": error, "reason": reason or errors.ERRORS[error][1], **detail})
    return errors.exit_code(error)


def _root(value: str | None) -> Path:
    return Path(value).expanduser().resolve() if value else Path.cwd()


def _ledger(args: argparse.Namespace, project_root: Path) -> Path:
    """Where this command's tasks live: ``--task-root``, else under the project."""
    value = getattr(args, "task_root", None)
    return Path(value).expanduser().resolve() if value else tasks.task_root(project_root)


def _load_task(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    task_dir = _ledger(args, _root(args.project_root)) / args.task_id
    return task_dir, store.read_json(task_dir / "state.json")


def _caller(args: argparse.Namespace) -> tasks.Owner:
    """Who is asking: a bridge naming its owner id with ``--owner``, else the session.

    Only the flag makes a bridge caller. An inherited environment variable once
    turned a Claude Code session into one without anyone noticing.
    """
    owner = getattr(args, "owner", None)
    if owner:
        return ("bridge", owner)
    return ("session", os.environ.get("CLAUDE_CODE_SESSION_ID") or None)


def cmd_submit(args: argparse.Namespace) -> int:
    if os.environ.get("DELEGATE_WORKER"):
        return _fail("nested_submit", parent_task=os.environ.get("DELEGATE_PARENT_TASK"))
    project_root = _root(args.project_root)
    config_path = (
        Path(args.config).expanduser().resolve()
        if args.config
        else tasks.settings_path(project_root)
    )
    try:
        config_bytes = config_path.read_bytes()
        settings = config.parse(config_bytes, config_path)
    except OSError:
        return _fail("no_settings", f"no settings at {config_path}")
    except config.ConfigError as error:
        return _fail("invalid_config", str(error))
    try:
        value = json.loads(Path(args.packet).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return _fail("invalid_packet", f"cannot read packet: {error}")

    try:
        handle = tasks.submit(
            project_root=project_root,
            settings=settings,
            role=args.role,
            title=args.title,
            value=value,
            worker=args.worker,
            fast=args.fast,
            caller=_caller(args),
            config_path=config_path,
            config_sha256=hashlib.sha256(config_bytes).hexdigest(),
            ledger=_ledger(args, project_root),
            allow_unhealthy=args.allow_unhealthy,
        )
    except tasks.SubmitRefused as refused:
        return _fail(refused.code, str(refused), **refused.detail)
    except config.ConfigError as error:
        return _fail("invalid_config", str(error))
    except ValueError as error:
        return _fail("invalid_packet", str(error))
    _print(handle)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    try:
        _, state = _load_task(args)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return _fail("task_not_found", str(error))
    _print(state)
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    try:
        task_dir, state = _load_task(args)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return _fail("task_not_found", str(error))
    caller = _caller(args)
    if not tasks.may_act(state, caller):
        return _fail("owner_mismatch")
    if state["status"] not in events.TERMINAL_STATES:
        return _fail(
            "task_not_terminal",
            status=state["status"],
            next_action="end_turn",
            completion_delivery="async_rewake",
        )

    result: Any = None
    if state["status"] in {"completed", "decision_needed", "unverified"} or (
        state["status"] == "degraded" and state.get("recovery_status") == "usable"
    ):
        result = envelope.chair_display(store.read_json(Path(state["result_path"])))
    state, claimed = events.claim_delivery(
        task_dir, delivered_to={"kind": caller[0], "id": caller[1]}
    )
    if not claimed:
        return _fail("already_delivered", delivered_at=state["delivered_at"])
    delivery = {
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
        "failure_message": state.get("failure_message"),
        "runtime_warnings": state.get("root_entry_warnings", []),
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
    remaining = _remaining_watch_tasks(task_dir)
    if remaining:
        delivery["completion_delivery"] = "async_rewake"
        delivery["watch_tasks"] = remaining
    _print(delivery)
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    try:
        task_dir, state = _load_task(args)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return _fail("task_not_found", str(error))
    if not tasks.may_act(state, _caller(args)):
        return _fail("owner_mismatch")
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
        "task_dir": state["task_dir"],
        "title": state["title"],
        "role": state["role"],
        "status": state["status"],
        "terminal_reason": state.get("terminal_reason"),
        "collected": bool(state.get("delivered_at")),
    }


COLLECT_NEXT_ACTION = (
    "collect each ready task once: delegate collect <task_id> --task-root <parent of its task_dir>"
)


def _next_action(lines: list[dict[str, Any]]) -> str | None:
    return COLLECT_NEXT_ACTION if lines else None


def _read_states(task_dirs: Iterable[Path]) -> list[dict[str, Any]]:
    """The states of these tasks; one whose directory is missing or unreadable is skipped."""
    found = []
    for task_dir in task_dirs:
        try:
            found.append(store.read_json(task_dir / "state.json"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return found


def _ledger_dirs(ledger: Path) -> list[Path]:
    return sorted(path.parent for path in ledger.glob("*/state.json"))


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


def _is_ready(state: dict[str, Any]) -> bool:
    return state["status"] in events.TERMINAL_STATES and not state.get("delivered_at")


def cmd_reconcile(args: argparse.Namespace, session_id: str | None = None) -> int:
    # A manual call is a session unless it names an owner, and is shown only what
    # it could collect: offering it another kind's task ends in owner_mismatch.
    # A hook call is shown the tasks its session submitted, wherever they are kept.
    task_dirs = _ledger_dirs(_ledger(args, _root(args.project_root)))
    if session_id:
        task_dirs += [*tasks.pending_completion_watches(session_id), *_session_watches(session_id)]
    caller = _caller(args)

    def wanted(state: dict[str, Any]) -> bool:
        if session_id is None:
            return tasks.may_act(state, caller)
        return bool(session_id) and tasks.owner_of(state) == ("session", session_id)

    reconciled = [
        _reconcile_state(state) for state in _read_states(dict.fromkeys(task_dirs)) if wanted(state)
    ]
    ready = [_line(state) for state in reconciled if _is_ready(state)]
    _print(
        {
            "tasks": [_line(state) for state in reconciled],
            "ready": ready,
            "next_action": _next_action(ready),
        }
    )
    return 0


def cmd_hook_reconcile(args: argparse.Namespace) -> int:
    try:
        session_id = json.load(sys.stdin).get("session_id")
    except (ValueError, AttributeError):
        session_id = None
    return cmd_reconcile(args, session_id if isinstance(session_id, str) else "")


#: An ``asyncRewake`` hook wakes the session on exit code 2 and on no other code,
#: and the woken session is handed the hook's output in place of a task list. So 2
#: is reserved, here and in the launcher, for a task being ready.
WAKE = 2
QUIET = 0


def _task_dirs_from_stdout(stdout: str) -> list[Path]:
    """The task directories named by the delegate handles in a command's output."""
    found: dict[Path, None] = {}
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict) or value.get("completion_delivery") != "async_rewake":
            continue
        for candidate in [value, *value.get("watch_tasks", [])]:
            task_dir = tasks.entry_dir(candidate)
            if task_dir is not None:
                found[task_dir] = None
    return list(found)


def _watch_entries(task_dirs: Iterable[Path]) -> list[dict[str, str]]:
    return [{"task_dir": str(task_dir)} for task_dir in task_dirs]


def _watch_group_path(session_id: str, watched: list[Path]) -> Path:
    digest = hashlib.sha256(json.dumps(_watch_entries(sorted(watched))).encode()).hexdigest()
    return tasks.watch_registry_dir(session_id) / f"{digest}.json"


def _session_watch_files(session_id: str) -> list[tuple[Path, list[Path]]]:
    """Each watch group this session remembered, with the directories it names."""
    found = []
    for path in sorted(tasks.watch_registry_dir(session_id).glob("*.json")):
        try:
            value = store.read_json(path)
            entries = value["watch_tasks"]
        except (KeyError, OSError, ValueError, json.JSONDecodeError):
            continue
        if value.get("session_id") == session_id and isinstance(entries, list):
            found.append((path, [d for d in map(tasks.entry_dir, entries) if d is not None]))
    return found


def _session_watches(session_id: str) -> list[Path]:
    return [task_dir for _, task_dirs in _session_watch_files(session_id) for task_dir in task_dirs]


def _prune_session_watches(session_id: str) -> None:
    for path, task_dirs in _session_watch_files(session_id):
        pending = _pending_session_watches(task_dirs)
        if pending:
            store.write_json(
                path, {"session_id": session_id, "watch_tasks": _watch_entries(pending)}
            )
        else:
            path.unlink(missing_ok=True)


def _owned_by_session(task_dirs: Iterable[Path], session_id: str) -> list[Path]:
    """These task directories, deduplicated, when they hold a task this session submitted."""
    owned: dict[Path, None] = {}
    for task_dir in task_dirs:
        for state in _read_states([task_dir]):
            if state.get("task_id") == task_dir.name and tasks.owner_of(state) == (
                "session",
                session_id,
            ):
                owned[task_dir] = None
    return list(owned)


def _pending_session_watches(task_dirs: list[Path]) -> list[Path]:
    """The watched tasks still running or waiting to be collected."""
    pending = []
    for task_dir in task_dirs:
        for state in _read_states([task_dir]):
            state = _reconcile_state(state)
            if state["status"] in events.ACTIVE_STATES or _is_ready(state):
                pending.append(task_dir)
    return pending


def cmd_watch(args: argparse.Namespace) -> int:
    """Wait until something is worth waking the session for.

    Exit ``WAKE`` means there is a finished, uncollected task. Exit ``QUIET`` means
    the session should be left alone.
    """
    return _watch(_ledger(args, _root(args.project_root)), _caller(args))


def cmd_hook_watch(args: argparse.Namespace) -> int:
    """Maintain this Claude Code session's delegated-task completion watch."""
    try:
        hook_input = json.load(sys.stdin)
        event_name = hook_input.get("hook_event_name")
        if (
            event_name not in {"PostToolUse", "PostToolUseFailure"}
            or hook_input.get("tool_name") != "Bash"
        ):
            raise ValueError("not a Bash completion hook")
        session_id = hook_input["session_id"]
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("missing session_id")
        output = (
            hook_input["tool_response"]["stdout"]
            if event_name == "PostToolUse"
            else hook_input["error"]
        )
        handles = _task_dirs_from_stdout(output)
    except (KeyError, TypeError, OSError, ValueError, json.JSONDecodeError):
        _print({"ready": []})
        return QUIET
    registered = tasks.pending_completion_watches(session_id)
    submitted = _owned_by_session([*handles, *registered], session_id)
    watched = _owned_by_session([*_session_watches(session_id), *submitted], session_id)
    _remember_watch_group(session_id, submitted)
    tasks.clear_pending_completion_watches(session_id, registered)
    result = _watch_tasks(watched)
    if result == WAKE:
        _prune_session_watches(session_id)
    return result


def _remember_watch_group(session_id: str, watched: list[Path]) -> None:
    """Record which tasks were submitted together, in each task and in the session."""
    if not watched:
        return
    group = {"watch_tasks": _watch_entries(watched)}
    for task_dir in watched:
        store.write_json(task_dir / "watch-group.json", group)
    store.write_json(_watch_group_path(session_id, watched), {"session_id": session_id, **group})


def _remaining_watch_tasks(task_dir: Path) -> list[dict[str, str]]:
    """The other tasks watched together with this one that have not been collected."""
    try:
        group = store.read_json(task_dir / "watch-group.json")["watch_tasks"]
    except (KeyError, OSError, ValueError, json.JSONDecodeError):
        return []
    return [
        {"task_id": state["task_id"], "task_dir": str(other)}
        for other in map(tasks.entry_dir, group)
        if other is not None and other != task_dir
        for state in _read_states([other])
        if not state.get("delivered_at")
    ]


def _watch(ledger: Path, caller: tasks.Owner) -> int:
    # One watcher per caller, so a bridge's watch does not silence a session's.
    name = "watch.lock"
    if caller[0] == "bridge":
        name = f"watch-{hashlib.sha256(str(caller[1]).encode()).hexdigest()[:16]}.lock"
    try:
        with liveness.hold(ledger / name):
            return _wait(lambda: _ledger_states(ledger, caller))
    except liveness.AlreadyHeld:
        # Deduplicated submissions can return the same active task handle.
        _print({"ready": []})
        return QUIET


def _ledger_states(ledger: Path, caller: tasks.Owner) -> list[dict[str, Any]]:
    return [state for state in _read_states(_ledger_dirs(ledger)) if tasks.may_act(state, caller)]


def _watch_tasks(watched: list[Path]) -> int:
    with ExitStack() as locks:
        accepted = []
        for task_dir in watched:
            try:
                locks.enter_context(liveness.hold(task_dir / "watch.lock"))
            except (liveness.AlreadyHeld, OSError):
                continue
            accepted.append(task_dir)
        if not accepted:
            _print({"ready": []})
            return QUIET
        return _wait(lambda: _read_states(accepted))


def _wait(read: Callable[[], list[dict[str, Any]]]) -> int:
    """Poll until a task is ready to collect (``WAKE``) or none is left running (``QUIET``)."""
    deadline = time.monotonic() + float(os.environ.get("DELEGATE_WATCH_SEC", "3600"))
    while True:
        states = [_reconcile_state(state) for state in read()]
        ready = [state for state in states if _is_ready(state)]
        if ready:
            _print({"ready": [_line(state) for state in ready], "next_action": COLLECT_NEXT_ACTION})
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
        target.add_argument(
            "--task-root",
            default=None,
            help="directory holding task records (default: <project-root>/.claude/logs/delegate)",
        )
        return target

    submit = with_root(sub.add_parser("submit", help="start a detached worker"))
    submit.add_argument("--role", required=True)
    submit.add_argument("--title", required=True)
    submit.add_argument("--packet", required=True, help="path to the task packet JSON")
    submit.add_argument("--worker", default=None)
    submit.add_argument(
        "--allow-unhealthy",
        action="store_true",
        help="submit even though the worker's last tasks all failed for the worker's own reasons",
    )
    submit.add_argument(
        "--config",
        default=None,
        help="settings file to use (default: <project-root>/.claude/delegate.toml)",
    )
    fast_flags = submit.add_mutually_exclusive_group()
    fast_flags.add_argument("--fast", dest="fast", action="store_const", const=True, default=None)
    fast_flags.add_argument("--no-fast", dest="fast", action="store_const", const=False)
    submit.add_argument(
        "--owner",
        default=None,
        help="own the task as a bridge; only this id may collect or cancel it",
    )
    submit.set_defaults(func=cmd_submit)

    status = with_root(sub.add_parser("status", help="read one task's state"))
    status.add_argument("task_id")
    status.set_defaults(func=cmd_status)

    collect = with_root(sub.add_parser("collect", help="take delivery of a finished task"))
    collect.add_argument("task_id")
    collect.add_argument("--owner", default=None, help="bridge owner id")
    collect.set_defaults(func=cmd_collect)

    cancel = with_root(sub.add_parser("cancel", help="stop a task"))
    cancel.add_argument("task_id")
    cancel.add_argument("--owner", default=None, help="bridge owner id")
    cancel.set_defaults(func=cmd_cancel)

    reconcile = with_root(sub.add_parser("reconcile", help="classify tasks whose worker is gone"))
    reconcile.add_argument(
        "--owner", default=None, help="list only this bridge owner's tasks (default: session tasks)"
    )
    reconcile.set_defaults(func=cmd_reconcile)

    hook_reconcile = with_root(sub.add_parser("_hook-reconcile", help=argparse.SUPPRESS))
    hook_reconcile.set_defaults(func=cmd_hook_reconcile)

    watch = with_root(sub.add_parser("watch", help="wait until a task is worth collecting"))
    watch.add_argument(
        "--owner", default=None, help="wait for this bridge owner's tasks (default: session tasks)"
    )
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
