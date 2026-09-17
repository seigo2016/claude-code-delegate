"""Waking Claude Code when a task finishes, and only then.

The point of delegating is that the session stops thinking about the task. So
nobody polls: a hook waits in the background and wakes the session exactly when
there is something to collect. Waking with nothing to say would put the cost back.

The exit code is not ours to choose. An ``asyncRewake`` hook wakes the session on
exit code 2 and on nothing else, and every other code is filed as a hook that ran
without incident.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from conftest import Workspace
from delegate import cli, events, liveness, store, tasks

WAKE = 2
QUIET = 0


def watch(workspace: Workspace, seconds: str = "1") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "delegate", "watch"],
        capture_output=True,
        text=True,
        env={**workspace.env, "DELEGATE_WATCH_SEC": seconds},
        cwd=workspace.repo,
    )


def hook_watch(
    workspace: Workspace,
    tool_stdout: str,
    *,
    cwd: Path,
    seconds: str = "1",
    session_id: str = "session-1",
    event_name: str = "PostToolUse",
) -> subprocess.CompletedProcess[str]:
    hook_input = {
        "session_id": session_id,
        "hook_event_name": event_name,
        "tool_name": "Bash",
    }
    if event_name == "PostToolUse":
        hook_input["tool_response"] = {
            "stdout": tool_stdout,
            "stderr": "",
            "interrupted": False,
            "isImage": False,
        }
    else:
        hook_input["error"] = tool_stdout
    return subprocess.run(
        [sys.executable, "-m", "delegate", "_hook-watch"],
        input=json.dumps(hook_input),
        capture_output=True,
        text=True,
        env={
            **workspace.env,
            "DELEGATE_WATCH_SEC": seconds,
            "DELEGATE_WATCH_ROOT": str(workspace.repo / ".delegate-watch-sessions"),
        },
        cwd=cwd,
    )


def test_with_no_tasks_at_all_nothing_is_woken(workspace: Workspace) -> None:
    result = watch(workspace)

    assert result.returncode == QUIET
    assert json.loads(result.stdout)["ready"] == []


def test_the_hook_watches_the_root_returned_by_cross_repo_submit(
    workspace: Workspace, tmp_path: Path
) -> None:
    handle = workspace.submit()

    result = hook_watch(workspace, json.dumps(handle), cwd=tmp_path, seconds="20")

    assert result.returncode == WAKE
    ready = json.loads(result.stdout)["ready"]
    assert ready[0]["task_id"] == handle["task_id"]
    assert ready[0]["project_root"] == str(workspace.repo)


def test_the_hook_accepts_a_handle_after_packet_setup_output(
    workspace: Workspace, tmp_path: Path
) -> None:
    handle = workspace.submit()

    result = hook_watch(
        workspace,
        f"packet created\n{json.dumps(handle)}",
        cwd=tmp_path,
        seconds="20",
    )

    assert result.returncode == WAKE
    assert json.loads(result.stdout)["ready"][0]["task_id"] == handle["task_id"]


def test_the_hook_accepts_a_handle_before_other_bash_output(
    workspace: Workspace, tmp_path: Path
) -> None:
    handle = workspace.submit()

    result = hook_watch(
        workspace,
        f"{json.dumps(handle)}\npacket retained at /tmp/task.json",
        cwd=tmp_path,
        seconds="20",
    )

    assert result.returncode == WAKE
    assert json.loads(result.stdout)["ready"][0]["task_id"] == handle["task_id"]


def test_the_failure_hook_watches_a_task_started_before_later_bash_failure(
    workspace: Workspace, tmp_path: Path
) -> None:
    handle = workspace.submit()

    result = hook_watch(
        workspace,
        f'Exit code 1\n{json.dumps(handle)}\n{{"error": "later command failed"}}',
        cwd=tmp_path,
        seconds="20",
        event_name="PostToolUseFailure",
    )

    assert result.returncode == WAKE
    assert json.loads(result.stdout)["ready"][0]["task_id"] == handle["task_id"]


def test_the_hook_watches_every_handle_from_one_bash_result(
    workspace: Workspace, tmp_path: Path
) -> None:
    workspace.mode("silent_hang")
    first = workspace.submit(title="first")
    deadline = time.monotonic() + 2
    while workspace.run("status", first["task_id"])["status"] != "running":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    while not workspace.calls():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    workspace.mode("success")
    second = workspace.submit(title="second")

    result = hook_watch(
        workspace,
        f"{json.dumps(first)}\n{json.dumps(second)}",
        cwd=tmp_path,
        seconds="20",
    )

    assert result.returncode == WAKE
    assert json.loads(result.stdout)["ready"][0]["task_id"] == second["task_id"]
    workspace.run("cancel", first["task_id"])


def test_collect_rearms_the_remaining_tasks_when_its_output_is_filtered(
    workspace: Workspace, tmp_path: Path
) -> None:
    workspace.mode("silent_hang")
    first = workspace.submit(title="first")
    deadline = time.monotonic() + 2
    while workspace.run("status", first["task_id"])["status"] != "running":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    while not workspace.calls():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    workspace.mode("success")
    second = workspace.submit(title="second")
    hook_watch(
        workspace,
        f"{json.dumps(first)}\n{json.dumps(second)}",
        cwd=tmp_path,
        seconds="20",
    )

    workspace.run("collect", second["task_id"])
    hook_input = {
        "session_id": "session-1",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_response": {"stdout": '{"status":"completed"}'},
    }
    result = subprocess.Popen(
        [sys.executable, "-m", "delegate", "_hook-watch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **workspace.env,
            "DELEGATE_WATCH_SEC": "20",
            "DELEGATE_WATCH_ROOT": str(workspace.repo / ".delegate-watch-sessions"),
        },
        cwd=tmp_path,
    )
    assert result.stdin is not None
    result.stdin.write(json.dumps(hook_input))
    result.stdin.close()
    deadline = time.monotonic() + 2
    task_lock = workspace.repo / ".claude" / "logs" / "delegate" / first["task_id"] / "watch.lock"
    while not liveness.worker_alive(task_lock) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert liveness.worker_alive(task_lock)
    workspace.run("cancel", first["task_id"])
    result.wait(timeout=5)

    assert result.returncode == WAKE
    assert result.stdout is not None
    assert json.loads(result.stdout.read())["ready"][0]["task_id"] == first["task_id"]


@pytest.mark.parametrize("terminal_status", sorted(events.TERMINAL_STATES))
def test_completion_between_notification_and_pruning_is_rearmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, terminal_status: str
) -> None:
    monkeypatch.setenv("DELEGATE_WATCH_ROOT", str(tmp_path / "registry"))
    monkeypatch.setenv("DELEGATE_WATCH_SEC", "0")
    watched = [(tmp_path, "first"), (tmp_path, "second")]
    for root, task_id in watched:
        task_dir = tasks.task_root(root) / task_id
        store.write_json(
            task_dir / "state.json",
            {
                "project_root": str(root),
                "task_id": task_id,
                "task_dir": str(task_dir),
                "lock_path": str(task_dir / "worker.lock"),
                "title": task_id,
                "role": "repro-runner",
                "status": "completed" if task_id == "first" else "running",
                "delivered_at": None,
            },
        )
    cli._remember_session_watch_group("session-1", watched)
    notifications = []
    second_path = tasks.task_root(tmp_path) / "second" / "state.json"

    def notify(value: dict[str, object]) -> None:
        notifications.append(value)
        state = store.read_json(second_path)
        state["status"] = terminal_status
        store.write_json(second_path, state)

    monkeypatch.setattr(cli, "_print", notify)
    with liveness.hold(tasks.task_root(tmp_path) / "second" / "worker.lock"):
        assert cli._watch_tasks(watched) == WAKE
    assert [item["task_id"] for item in notifications[0]["ready"]] == ["first"]
    cli._prune_session_watches("session-1")

    first_path = tasks.task_root(tmp_path) / "first" / "state.json"
    first_state = store.read_json(first_path)
    first_state["delivered_at"] = "2026-09-17T00:00:00+00:00"
    store.write_json(first_path, first_state)
    cli._prune_session_watches("session-1")
    remaining = cli._read_session_watches("session-1")
    assert remaining == [{"project_root": str(tmp_path), "task_id": "second"}]
    assert cli._watch_tasks(cli._watch_tasks_from_handles(remaining)) == WAKE
    assert [item["task_id"] for item in notifications[-1]["ready"]] == ["second"]

    state = store.read_json(second_path)
    state["delivered_at"] = "2026-09-17T00:01:00+00:00"
    store.write_json(second_path, state)
    cli._prune_session_watches("session-1")
    assert cli._read_session_watches("session-1") == []
    assert list(cli._watch_registry_dir("session-1").glob("*.json")) == []


def test_the_hook_ignores_json_that_does_not_name_a_task(
    workspace: Workspace, tmp_path: Path
) -> None:
    result = hook_watch(
        workspace,
        json.dumps(
            {
                "completion_delivery": "async_rewake",
                "project_root": str(workspace.repo),
                "task_id": "not-a-task",
            }
        ),
        cwd=tmp_path,
    )

    assert result.returncode == QUIET
    assert json.loads(result.stdout)["ready"] == []


def test_the_hook_ignores_a_bash_result_that_is_not_a_delegate_handle(
    workspace: Workspace, tmp_path: Path
) -> None:
    result = hook_watch(workspace, "ordinary command output", cwd=tmp_path)

    assert result.returncode == QUIET
    assert json.loads(result.stdout)["ready"] == []


def test_each_submitted_task_keeps_its_own_completion_watcher(
    workspace: Workspace, tmp_path: Path
) -> None:
    workspace.mode("silent_hang")
    first = workspace.submit(title="first")
    first_input = json.dumps(
        {
            "session_id": "session-1",
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_response": {"stdout": json.dumps(first)},
        }
    )
    first_hook = subprocess.Popen(
        [sys.executable, "-m", "delegate", "_hook-watch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **workspace.env,
            "DELEGATE_WATCH_SEC": "20",
            "DELEGATE_WATCH_ROOT": str(workspace.repo / ".delegate-watch-sessions"),
        },
        cwd=tmp_path,
    )
    assert first_hook.stdin is not None
    first_hook.stdin.write(first_input)
    first_hook.stdin.close()
    deadline = time.monotonic() + 2
    task_lock = workspace.repo / ".claude" / "logs" / "delegate" / first["task_id"] / "watch.lock"
    while not liveness.worker_alive(task_lock) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert liveness.worker_alive(task_lock)
    while not workspace.calls() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert workspace.calls()

    workspace.mode("success")
    second = workspace.submit(title="second")
    second_result = hook_watch(workspace, json.dumps(second), cwd=tmp_path, seconds="20")

    assert second_result.returncode == WAKE
    assert json.loads(second_result.stdout)["ready"][0]["task_id"] == second["task_id"]

    workspace.run("cancel", first["task_id"])
    first_hook.wait(timeout=5)
    assert first_hook.returncode == WAKE
    assert first_hook.stdout is not None
    first_ready = json.loads(first_hook.stdout.read())["ready"][0]
    assert first_ready["task_id"] == first["task_id"]
    assert first_ready["status"] == "cancelled"


def test_a_task_still_running_does_not_wake_the_session(workspace: Workspace) -> None:
    workspace.mode("silent_hang")
    handle = workspace.submit()

    result = watch(workspace)

    assert result.returncode == QUIET
    workspace.run("cancel", handle["task_id"])


def test_a_finished_task_wakes_the_session_and_is_named(workspace: Workspace) -> None:
    handle = workspace.submit()

    result = watch(workspace, seconds="20")

    assert result.returncode == WAKE
    ready = json.loads(result.stdout)["ready"]
    assert [entry["task_id"] for entry in ready] == [handle["task_id"]]
    assert ready[0]["status"] == "completed"


def test_a_failure_wakes_the_session_just_as_a_success_does(workspace: Workspace) -> None:
    workspace.mode("nonzero_exit")
    workspace.submit()

    result = watch(workspace, seconds="20")

    assert result.returncode == WAKE
    assert json.loads(result.stdout)["ready"][0]["status"] == "failed"


def test_a_vanished_worker_is_reconciled_and_wakes_the_session(workspace: Workspace) -> None:
    task_dir = workspace.repo / ".claude" / "logs" / "delegate" / "vanished"
    task_dir.mkdir(parents=True)
    (task_dir / "state.json").write_text(
        json.dumps(
            {
                "task_id": "vanished",
                "project_root": str(workspace.repo),
                "title": "t",
                "role": "artifact-auditor",
                "status": "starting",
                "created_at": (datetime.now().astimezone() - timedelta(minutes=1)).isoformat(),
                "task_dir": str(task_dir),
                "lock_path": str(task_dir / "worker.lock"),
                "result_path": str(task_dir / "result.json"),
                "delivered_at": None,
            }
        ),
        encoding="utf-8",
    )

    result = watch(workspace)

    assert result.returncode == WAKE
    assert json.loads(result.stdout)["ready"][0]["status"] == "orphaned"


def test_an_already_collected_task_does_not_wake_the_session_again(workspace: Workspace) -> None:
    handle = workspace.submit()
    assert watch(workspace, seconds="20").returncode == WAKE
    workspace.run("collect", handle["task_id"])

    result = watch(workspace)

    assert result.returncode == QUIET


def test_reconcile_reports_a_short_line_per_task_not_the_whole_state(
    workspace: Workspace,
) -> None:
    workspace.submit()

    reported = workspace.run("reconcile")["tasks"][0]

    assert set(reported) == {
        "task_id",
        "project_root",
        "title",
        "role",
        "status",
        "terminal_reason",
        "collected",
    }


def test_only_one_watcher_per_project_waits_at_a_time(workspace: Workspace) -> None:
    # A watcher starts after every Bash call, so without a claim they pile up
    # and each one wakes the session about the same finished task.
    workspace.submit()
    assert watch(workspace, seconds="20").returncode == WAKE

    lock = workspace.repo / ".claude" / "logs" / "delegate" / "watch.lock"
    with liveness.hold(lock):
        second = watch(workspace, seconds="20")

    assert second.returncode == QUIET
