"""Starting a worker, and what happens when it cannot start.

A task that never ran must say why. Reporting it as a worker that vanished hides
the cause and invites the caller to retry the same broken thing.
"""

from __future__ import annotations

import json

from conftest import Workspace
from delegate import events


def test_a_backend_that_does_not_exist_is_refused_before_a_task_is_made(
    workspace: Workspace,
) -> None:
    workspace.settings.write_text(
        workspace.settings.read_text(encoding="utf-8").replace(
            'adapter = "codex"', 'adapter = "does-not-exist"'
        ),
        encoding="utf-8",
    )

    refusal = workspace.submit()

    assert "does-not-exist" in refusal["error"]
    assert not (workspace.repo / ".claude" / "logs" / "delegate").exists()


def test_a_backend_that_cannot_be_launched_is_a_failure_not_a_disappearance(
    workspace: Workspace,
) -> None:
    workspace.remove_worker()

    handle = workspace.submit()

    from test_lifecycle import wait_for_terminal

    state = wait_for_terminal(workspace, handle["task_id"])
    assert state["status"] == "failed"
    assert state["terminal_reason"] == "launch_error"


def test_a_task_that_has_only_just_been_handed_over_is_not_declared_lost(
    workspace: Workspace,
) -> None:
    # The supervisor takes a moment to claim its lock. In that window the task
    # has no lock and is not gone either.
    task_dir = workspace.repo / ".claude" / "logs" / "delegate" / "just-handed-over"
    task_dir.mkdir(parents=True)
    (task_dir / "state.json").write_text(
        json.dumps(
            {
                "task_id": "just-handed-over",
                "title": "t",
                "role": "artifact-auditor",
                "status": "starting",
                "created_at": events.now_iso(),
                "task_dir": str(task_dir),
                "lock_path": str(task_dir / "worker.lock"),
                "result_path": str(task_dir / "result.json"),
            }
        ),
        encoding="utf-8",
    )

    (reconciled,) = workspace.run("reconcile")["tasks"]

    assert reconciled["status"] == "starting"


def test_a_worker_that_vanished_after_writing_a_result_is_degraded_not_orphaned(
    workspace: Workspace,
) -> None:
    # The two say different things to the caller: one produced nothing, the
    # other left something worth reading.
    task_dir = workspace.repo / ".claude" / "logs" / "delegate" / "vanished"
    task_dir.mkdir(parents=True)
    (task_dir / "result.json").write_text("{}", encoding="utf-8")
    (task_dir / "state.json").write_text(
        json.dumps(
            {
                "task_id": "vanished",
                "title": "t",
                "role": "artifact-auditor",
                "status": "running",
                "created_at": events.now_iso(),
                "task_dir": str(task_dir),
                "lock_path": str(task_dir / "worker.lock"),
                "result_path": str(task_dir / "result.json"),
            }
        ),
        encoding="utf-8",
    )

    (reconciled,) = workspace.run("reconcile")["tasks"]

    assert reconciled["status"] == "degraded"
    assert reconciled["terminal_reason"] == "worker_gone_with_result"
