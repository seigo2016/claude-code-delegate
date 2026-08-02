"""What a task said it would write, checked against worker-reported changes.

The shared work tree can change for reasons outside one worker. Such changes
are recorded without being attributed to that worker.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from conftest import PACKET, Workspace
from delegate import workspace as workspace_state
from test_lifecycle import wait_for_terminal

SCOPED = json.dumps({**json.loads(PACKET), "allowed_writes": ["allowed.txt"]})


def test_worker_paths_keep_the_worktree_name_of_a_symlink(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)

    paths = workspace_state.relative_paths(root, (str(root / "linked" / "file.txt"),))

    assert paths == {"linked/file.txt"}


def test_the_declared_scope_also_decides_what_the_worker_is_permitted(
    workspace: Workspace,
) -> None:
    reading = workspace.submit()
    writing = workspace.submit(packet=SCOPED, role="bounded-implementer")

    assert workspace.run("status", reading["task_id"])["writes_allowed"] is False
    assert workspace.run("status", writing["task_id"])["writes_allowed"] is True


def test_a_role_that_runs_commands_is_recorded_as_one(workspace: Workspace) -> None:
    auditing = workspace.submit()
    verifying = workspace.submit(role="verification-runner")

    assert workspace.run("status", auditing["task_id"])["runs_commands"] is False
    assert workspace.run("status", verifying["task_id"])["runs_commands"] is True


def test_writing_inside_the_declared_scope_is_accepted(workspace: Workspace) -> None:
    workspace.mode("writes_allowed")

    handle = workspace.submit(packet=SCOPED, role="bounded-implementer")
    state = wait_for_terminal(workspace, handle["task_id"])

    assert state["status"] == "completed"
    assert state["write_scope_checked"] is True


def test_writing_outside_the_declared_scope_is_a_failure(workspace: Workspace) -> None:
    workspace.mode("writes_elsewhere")

    handle = workspace.submit(packet=SCOPED, role="bounded-implementer")
    state = wait_for_terminal(workspace, handle["task_id"])

    assert state["status"] == "failed"
    assert state["terminal_reason"] == "write_scope_violation"
    assert state["unauthorized_writes"] == ["unauthorized.txt"]


def test_a_concurrent_workspace_change_is_not_attributed_to_the_worker(
    workspace: Workspace,
) -> None:
    workspace.mode("wait_for_foreign_change")

    handle = workspace.submit()
    deadline = time.monotonic() + 5
    while workspace.run("status", handle["task_id"])["status"] != "running":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    (workspace.repo / "foreign.txt").write_text("written by another process")
    state = wait_for_terminal(workspace, handle["task_id"])

    assert state["status"] == "completed"
    assert state["write_scope_checked"] is False
    assert state["unauthorized_writes"] == []
    assert state["unattributed_workspace_changes"] == ["foreign.txt"]
    collected = workspace.run("collect", handle["task_id"])
    assert collected["result"]["status"] == "completed"


def test_a_repository_without_git_says_the_scope_was_not_checked(workspace: Workspace) -> None:
    import shutil

    shutil.rmtree(workspace.repo / ".git")
    workspace.mode("writes_elsewhere")

    handle = workspace.submit(packet=SCOPED, role="bounded-implementer")
    state = wait_for_terminal(workspace, handle["task_id"])

    assert state["status"] == "completed"
    assert state["write_scope_checked"] is False
