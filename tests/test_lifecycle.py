"""One task, from submit to collection, including every way it can end badly.

The contract that matters: submitting returns immediately, a finished task is
delivered exactly once, and nothing that went wrong is ever reported as success.
"""

from __future__ import annotations

import json
import time
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


def submit(workspace: Workspace, *extra: str, packet: str = PACKET, role: str = "artifact-auditor"):
    (workspace.repo / "packet.json").write_text(packet, encoding="utf-8")
    return workspace.run(
        "submit",
        "--role",
        role,
        "--title",
        "count-tests",
        "--packet",
        "packet.json",
        *extra,
        expect_success=False,
    )


def wait_for_terminal(workspace: Workspace, task_id: str, timeout: float = 20) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = workspace.run("status", task_id, "--reason", "user-requested")
        if state["status"] not in {"queued", "starting", "running", "cancellation_requested"}:
            return state
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} never finished")


def test_submitting_returns_a_handle_without_waiting(workspace: Workspace) -> None:
    workspace.mode("silent_hang")

    handle = submit(workspace)

    assert handle["task_id"]
    assert handle["status"] in {"queued", "starting", "running"}
    assert (handle["model"], handle["effort"], handle["worker"]) == ("terra", "high", "codex")

    workspace.run("cancel", handle["task_id"])


def test_the_result_contract_is_written_beside_the_task_not_looked_up_on_disk(
    workspace: Workspace,
) -> None:
    handle = submit(workspace)
    state = wait_for_terminal(workspace, handle["task_id"])

    shipped = Path(state["task_dir"]) / "result.schema.json"
    assert shipped.exists(), "the worker must be handed a schema that travels with the task"
    assert str(shipped) in workspace.calls()[0]


def test_a_finished_task_is_delivered_once(workspace: Workspace) -> None:
    handle = submit(workspace)
    state = wait_for_terminal(workspace, handle["task_id"])
    assert state["status"] == "completed"

    envelope = workspace.run("collect", handle["task_id"])

    assert envelope["result"]["observed_facts"] == ["the fake worker ran"]
    assert envelope["status"] == "completed"

    again = workspace.run("collect", handle["task_id"], expect_success=False)
    assert again["error"] == "already_delivered"


def test_a_task_still_running_cannot_be_collected(workspace: Workspace) -> None:
    workspace.mode("silent_hang")
    handle = submit(workspace)

    refusal = workspace.run("collect", handle["task_id"], expect_success=False)

    assert refusal["error"] == "task_not_terminal"
    workspace.run("cancel", handle["task_id"])


def test_a_result_that_asks_for_a_decision_is_not_reported_as_completed(
    workspace: Workspace,
) -> None:
    workspace.mode("decision_needed")
    handle = submit(workspace)

    state = wait_for_terminal(workspace, handle["task_id"])

    assert state["status"] == "decision_needed"


def test_a_worker_that_exits_nonzero_is_a_failure(workspace: Workspace) -> None:
    workspace.mode("nonzero_exit")
    handle = submit(workspace)

    state = wait_for_terminal(workspace, handle["task_id"])

    assert state["status"] == "failed"
    assert state["terminal_reason"] == "nonzero_exit"


def test_a_backend_that_cannot_write_a_result_file_still_delivers_one(
    workspace: Workspace,
) -> None:
    # Only Codex can be handed an output path. OpenCode and claude return their
    # answer as the final message, so the result has to be taken from there.
    workspace.mode("result_in_message_only")
    handle = submit(workspace)

    state = wait_for_terminal(workspace, handle["task_id"])

    assert state["status"] == "completed"
    assert workspace.run("collect", handle["task_id"])["result"]["observed_facts"] == [
        "the fake worker ran"
    ]


def test_a_worker_that_writes_no_result_is_a_failure(workspace: Workspace) -> None:
    workspace.mode("empty_result")
    handle = submit(workspace)

    state = wait_for_terminal(workspace, handle["task_id"])

    assert state["status"] == "failed"
    assert state["terminal_reason"] == "empty_result"


def test_a_result_that_breaks_the_contract_is_a_failure_and_the_text_is_kept(
    workspace: Workspace,
) -> None:
    workspace.mode("invalid_result")
    handle = submit(workspace)

    state = wait_for_terminal(workspace, handle["task_id"])

    assert state["status"] == "failed"
    assert state["terminal_reason"] == "invalid_result"
    kept = Path(state["last_agent_message_path"]).read_text(encoding="utf-8")
    assert kept == "I did the thing"


def test_a_task_that_runs_out_of_time_records_where_it_stopped(workspace: Workspace) -> None:
    workspace.mode("tool_hang")
    handle = submit(workspace, "--timeout", "1")

    state = wait_for_terminal(workspace, handle["task_id"])

    assert state["status"] == "timeout"
    assert state["failure_class"] == "tool_stall"


def test_a_worker_that_finished_but_never_handed_back_a_result_is_named_as_such(
    workspace: Workspace,
) -> None:
    workspace.mode("finalization_hang")
    handle = submit(workspace, "--timeout", "1")

    state = wait_for_terminal(workspace, handle["task_id"])

    assert state["status"] == "timeout"
    assert state["failure_class"] == "finalization_timeout"
    assert Path(state["recovered_result_path"]).exists(), "a usable final message is kept aside"


def test_the_recovered_message_is_not_promoted_to_a_result(workspace: Workspace) -> None:
    workspace.mode("finalization_hang")
    handle = submit(workspace, "--timeout", "1")
    wait_for_terminal(workspace, handle["task_id"])

    envelope = workspace.run("collect", handle["task_id"])

    assert envelope["status"] == "timeout"
    assert envelope["result"] is None


def test_the_same_request_twice_reuses_the_first_task(workspace: Workspace) -> None:
    workspace.mode("silent_hang")
    first = submit(workspace)

    second = submit(workspace)

    assert second["task_id"] == first["task_id"]
    assert second["deduplicated"] is True

    third = submit(workspace, "--fresh")
    assert third["task_id"] != first["task_id"]

    workspace.run("cancel", first["task_id"])
    workspace.run("cancel", third["task_id"])


def test_host_only_work_never_starts_a_worker(workspace: Workspace) -> None:
    packet = json.dumps({**json.loads(PACKET), "host_only": True})

    refusal = submit(workspace, packet=packet)

    assert refusal["run_in"] == "claude-code"
    assert workspace.calls() == [], "no worker was started"


def test_a_role_that_must_declare_its_writes_is_refused_without_them(workspace: Workspace) -> None:
    refusal = submit(workspace, role="bounded-implementer")

    assert "allowed_writes" in refusal["error"]
    assert workspace.calls() == []


def test_a_cancelled_task_is_terminal_and_not_a_success(workspace: Workspace) -> None:
    workspace.mode("silent_hang")
    handle = submit(workspace)

    workspace.run("cancel", handle["task_id"])

    state = wait_for_terminal(workspace, handle["task_id"])
    assert state["status"] == "cancelled"


def test_a_worker_that_vanished_is_reclassified_rather_than_left_running(
    workspace: Workspace,
) -> None:
    workspace.mode("silent_hang")
    handle = submit(workspace)
    state = workspace.run("status", handle["task_id"], "--reason", "user-requested")
    task_dir = Path(state["task_dir"])

    # Kill the worker the way a reboot would: no chance to record anything.
    import signal

    while not (task_dir / "worker.lock").exists():
        time.sleep(0.02)
    os_pid = workspace.run("status", handle["task_id"], "--reason", "recovery")["pid"]
    import os

    os.killpg(os.getpgid(os_pid), signal.SIGKILL)
    time.sleep(0.2)

    reconciled = workspace.run("reconcile")

    assert reconciled["tasks"][0]["status"] == "orphaned"
