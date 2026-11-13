"""Do not keep sending work to a worker that is broken rather than the task."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from conftest import SETTINGS, Workspace
from delegate import store
from test_compare_workers import COMPARE
from test_lifecycle import wait_for_terminal

WORKER_UNHEALTHY = 7
BASE = datetime(2026, 11, 1, 12, 0, 0).astimezone()


def record(
    workspace: Workspace,
    task_id: str,
    *,
    worker: str = "codex",
    status: str = "failed",
    failure_class: str | None = "auth_failed",
    terminal_reason: str = "nonzero_exit",
    minute: int = 0,
) -> str:
    """Put a finished task into the default ledger as an earlier run would have left it."""
    stamp = (BASE + timedelta(minutes=minute)).isoformat(timespec="seconds")
    store.write_json(
        workspace.repo / ".claude" / "logs" / "delegate" / task_id / "state.json",
        {
            "task_id": task_id,
            "role": "artifact-auditor",
            "worker": worker,
            "status": status,
            "failure_class": failure_class,
            "terminal_reason": terminal_reason,
            "created_at": stamp,
            "terminal_at": stamp,
        },
    )
    return task_id


def failures(workspace: Workspace, count: int = 3, **fields: Any) -> list[str]:
    return [record(workspace, f"old-{i}", minute=i, **fields) for i in range(count)]


def refused(workspace: Workspace, *extra: str, role: str = "artifact-auditor") -> dict:
    out = workspace.submit(*extra, role=role)
    assert "task_id" not in out, out
    return out


def exit_code(workspace: Workspace, *extra: str, role: str = "artifact-auditor") -> int:
    import subprocess
    import sys

    (workspace.repo / "packet.json").write_text(
        json.dumps(
            {
                "objective": "Count the tests.",
                "read": ["tests"],
                "allowed_writes": [],
                "required_evidence": ["test count"],
                "host_only": False,
            }
        ),
        encoding="utf-8",
    )
    return subprocess.run(
        [sys.executable, "-m", "delegate", "submit", "--role", role, "--title", "x"]
        + ["--packet", "packet.json", *extra],
        capture_output=True,
        text=True,
        env=workspace.env,
        cwd=workspace.repo,
    ).returncode


def test_three_auth_failures_in_a_row_refuse_the_worker(workspace: Workspace) -> None:
    ids = failures(workspace)

    out = refused(workspace)

    assert out["error"] == "worker_unhealthy"
    assert (out["worker"], out["failure_class"]) == ("codex", "auth_failed")
    assert out["task_ids"] == ["old-2", "old-1", "old-0"]
    assert set(out["task_ids"]) == set(ids)
    assert exit_code(workspace) == WORKER_UNHEALTHY
    assert sorted(p.name for p in (workspace.repo / ".claude/logs/delegate").iterdir()) == sorted(
        ids
    )


def test_an_expired_key_seen_three_times_stops_the_fourth_submit(workspace: Workspace) -> None:
    workspace.settings.write_text(
        workspace.settings.read_text(encoding="utf-8").replace(
            'adapter = "codex"', 'adapter = "opencode"'
        ),
        encoding="utf-8",
    )
    workspace.mode("opencode_expired_api_key")
    done = []
    for index in range(3):
        handle = workspace.submit(title=f"try-{index}", expect_success=True)
        assert wait_for_terminal(workspace, handle["task_id"])["failure_class"] == "auth_failed"
        done.append(handle["task_id"])

    out = refused(workspace)
    assert out["error"] == "worker_unhealthy"
    assert sorted(out["task_ids"]) == sorted(done)

    handle = workspace.submit("--allow-unhealthy", title="deliberate", expect_success=True)
    assert wait_for_terminal(workspace, handle["task_id"])["failure_class"] == "auth_failed"


def test_launch_and_supervisor_errors_count_as_the_worker_being_broken(
    workspace: Workspace,
) -> None:
    failures(workspace, failure_class="launch_error", terminal_reason="launch_error")
    assert refused(workspace)["failure_class"] == "launch_error"


def test_supervisor_errors_group_whatever_exception_named_them(workspace: Workspace) -> None:
    for index, name in enumerate(["KeyError", "ValueError", "OSError"]):
        record(
            workspace,
            f"sup-{index}",
            failure_class=name,
            terminal_reason="supervisor_error",
            minute=index,
        )

    assert refused(workspace)["failure_class"] == "supervisor_error"


def test_a_failure_that_may_be_the_tasks_own_does_not_count(workspace: Workspace) -> None:
    for failure_class in (
        "nonzero_exit",
        "worker_api_error",
        "provider_capacity",
        "incomplete_work",
    ):
        failures(workspace, failure_class=failure_class)
        assert "task_id" in workspace.submit(title=failure_class, expect_success=True)
        for path in (workspace.repo / ".claude/logs/delegate").glob("old-*"):
            (path / "state.json").unlink()


def test_the_streak_must_be_three_consecutive_and_the_same(workspace: Workspace) -> None:
    record(workspace, "a", minute=0)
    record(workspace, "b", minute=1)
    record(workspace, "ok", status="completed", failure_class=None, minute=2)
    record(workspace, "c", minute=3)
    record(workspace, "d", minute=4)
    assert "task_id" in workspace.submit(title="one", expect_success=True)

    record(workspace, "e", minute=5, failure_class="launch_error", terminal_reason="launch_error")
    record(workspace, "f", minute=6)
    assert "task_id" in workspace.submit(title="two", expect_success=True)


def test_only_the_most_recent_three_are_looked_at(workspace: Workspace) -> None:
    failures(workspace)
    record(workspace, "recovered", status="completed", failure_class=None, minute=10)

    assert "task_id" in workspace.submit(expect_success=True)


def test_another_workers_failures_are_not_held_against_this_one(workspace: Workspace) -> None:
    failures(workspace, worker="opencode")

    assert "task_id" in workspace.submit(expect_success=True)


def test_allow_unhealthy_permits_a_deliberate_retry_and_records_it(workspace: Workspace) -> None:
    failures(workspace)
    handle = workspace.submit("--allow-unhealthy", expect_success=True)

    state = workspace.run("status", handle["task_id"])
    assert state["unhealthy_override"] == {
        "failure_class": "auth_failed",
        "task_ids": ["old-2", "old-1", "old-0"],
    }


def test_an_explicit_worker_is_checked_too(workspace: Workspace) -> None:
    workspace.settings.write_text(SETTINGS + COMPARE, encoding="utf-8")
    failures(workspace, worker="opencode")

    assert refused(workspace, "--worker", "opencode")["worker"] == "opencode"


def test_an_explicit_worker_is_not_swapped_even_in_a_comparing_role(
    workspace: Workspace,
) -> None:
    workspace.settings.write_text(SETTINGS + COMPARE, encoding="utf-8")
    failures(workspace, worker="opencode")

    out = refused(workspace, "--worker", "opencode", role="ab")

    assert (out["error"], out["worker"]) == ("worker_unhealthy", "opencode")


def test_a_comparison_moves_to_the_healthy_arm(workspace: Workspace) -> None:
    workspace.settings.write_text(SETTINGS + COMPARE, encoding="utf-8")
    workspace.mode("utterly_silent")
    failures(workspace, worker="codex")

    handle = workspace.submit(role="ab", expect_success=True)

    state = workspace.run("status", handle["task_id"])
    assert state["worker"] == "opencode"
    assert (state["route_arm"], state["route_policy"]) == ("opencode", "compare")
    assert state["route_fallback_from"] == "codex"


def test_a_comparison_with_both_arms_broken_is_refused(workspace: Workspace) -> None:
    workspace.settings.write_text(SETTINGS + COMPARE, encoding="utf-8")
    failures(workspace, worker="codex")
    for index in range(3):
        record(workspace, f"oc-{index}", worker="opencode", minute=index)

    out = refused(workspace, role="ab")

    assert out["error"] == "worker_unhealthy"
    assert out["worker"] == "codex"


def test_allow_unhealthy_keeps_the_arm_the_alternation_chose(workspace: Workspace) -> None:
    workspace.settings.write_text(SETTINGS + COMPARE, encoding="utf-8")
    workspace.mode("utterly_silent")
    failures(workspace, worker="codex")

    handle = workspace.submit("--allow-unhealthy", role="ab", expect_success=True)

    state = workspace.run("status", handle["task_id"])
    assert (state["worker"], state["route_fallback_from"]) == ("codex", None)


def test_path_helper_is_not_confused_by_missing_ledger(tmp_path: Path) -> None:
    from delegate import tasks

    assert tasks.recent_unhealthy(tmp_path / "nowhere", "codex") is None
