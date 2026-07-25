"""Waking Claude Code when a task finishes, and only then.

The point of delegating is that the session stops thinking about the task. So
nobody polls: a hook waits in the background and wakes the session exactly when
there is something to collect. Waking with nothing to say would put the cost back.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from conftest import Workspace

PACKET = json.dumps(
    {
        "objective": "Count the tests.",
        "read": ["tests"],
        "allowed_writes": [],
        "required_evidence": ["test count"],
        "host_only": False,
    }
)


def watch(workspace: Workspace, seconds: str = "1") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "delegate", "watch"],
        capture_output=True,
        text=True,
        env={**workspace.env, "DELEGATE_WATCH_SEC": seconds},
        cwd=workspace.repo,
    )


def submit(workspace: Workspace) -> dict:
    (workspace.repo / "packet.json").write_text(PACKET, encoding="utf-8")
    return workspace.run(
        "submit", "--role", "artifact-auditor", "--title", "t", "--packet", "packet.json"
    )


def test_with_no_tasks_at_all_nothing_is_woken(workspace: Workspace) -> None:
    result = watch(workspace)

    assert result.returncode != 0
    assert json.loads(result.stdout)["ready"] == []


def test_a_task_still_running_does_not_wake_the_session(workspace: Workspace) -> None:
    workspace.mode("silent_hang")
    handle = submit(workspace)

    result = watch(workspace)

    assert result.returncode != 0
    workspace.run("cancel", handle["task_id"])


def test_a_finished_task_wakes_the_session_and_is_named(workspace: Workspace) -> None:
    handle = submit(workspace)

    result = watch(workspace, seconds="20")

    assert result.returncode == 0
    ready = json.loads(result.stdout)["ready"]
    assert [entry["task_id"] for entry in ready] == [handle["task_id"]]
    assert ready[0]["status"] == "completed"


def test_a_failure_wakes_the_session_just_as_a_success_does(workspace: Workspace) -> None:
    workspace.mode("nonzero_exit")
    submit(workspace)

    result = watch(workspace, seconds="20")

    assert result.returncode == 0
    assert json.loads(result.stdout)["ready"][0]["status"] == "failed"


def test_an_already_collected_task_does_not_wake_the_session_again(workspace: Workspace) -> None:
    handle = submit(workspace)
    assert watch(workspace, seconds="20").returncode == 0
    workspace.run("collect", handle["task_id"])

    result = watch(workspace)

    assert result.returncode != 0


def test_reconcile_reports_a_short_line_per_task_not_the_whole_state(
    workspace: Workspace,
) -> None:
    handle = submit(workspace)
    deadline_state = workspace.run("status", handle["task_id"], "--reason", "watchdog")
    assert Path(deadline_state["task_dir"]).exists()

    reported = workspace.run("reconcile")["tasks"][0]

    assert set(reported) == {
        "task_id",
        "title",
        "role",
        "status",
        "terminal_reason",
        "collected",
    }
