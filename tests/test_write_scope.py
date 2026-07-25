"""What a task said it would write, checked against what it did write.

Stating a write scope in the prompt asks the worker to behave. Comparing the
repository before and after is what turns that request into an observation.
"""

from __future__ import annotations

import json

from conftest import PACKET, Workspace
from test_lifecycle import wait_for_terminal

SCOPED = json.dumps({**json.loads(PACKET), "allowed_writes": ["allowed.txt"]})


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


def test_a_repository_without_git_says_the_scope_was_not_checked(workspace: Workspace) -> None:
    import shutil

    shutil.rmtree(workspace.repo / ".git")
    workspace.mode("writes_elsewhere")

    handle = workspace.submit(packet=SCOPED, role="bounded-implementer")
    state = wait_for_terminal(workspace, handle["task_id"])

    assert state["status"] == "completed"
    assert state["write_scope_checked"] is False
