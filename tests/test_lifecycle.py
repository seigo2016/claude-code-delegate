"""One task, from submit to collection, including every way it can end badly.

The contract that matters: submitting returns immediately, a finished task is
delivered exactly once, and nothing that went wrong is ever reported as success.
"""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

import pytest

from conftest import PACKET, Workspace


def wait_for_terminal(workspace: Workspace, task_id: str, timeout: float = 20) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = workspace.run("status", task_id)
        if state["status"] not in {"queued", "starting", "running", "cancellation_requested"}:
            return state
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} never finished")


@pytest.mark.parametrize("isolated", [True, False])
def test_task_temp_and_root_warnings(workspace: Workspace, tmp_path: Path, isolated: bool) -> None:
    workspace.env["XDG_CACHE_HOME"] = str(tmp_path / "cache")
    workspace.env["FAKE_TEMP_RECORD"] = str(tmp_path / "temp-record")
    workspace.env["FAKE_ROOT"] = str(workspace.repo)
    workspace.settings.write_text(
        workspace.settings.read_text()
        .replace(
            'adapter = "codex"', f'adapter = "codex"\nisolate_home = {str(isolated).lower()}', 1
        )
        .replace(
            '[roles.bounded-implementer]\nlevel = "standard"',
            '[roles.bounded-implementer]\nlevel = "standard"\n'
            f'allowed_write_roots = ["{workspace.repo}"]',
        )
    )
    workspace.mode("temporary_and_root_entries")
    packet = json.dumps({**json.loads(PACKET), "allowed_writes": ["declared/output.txt"]})
    handle = workspace.submit(packet=packet, role="bounded-implementer")
    state = wait_for_terminal(workspace, handle["task_id"])
    temp = tmp_path / "cache" / "tmp" / handle["task_id"]
    assert Path(workspace.env["FAKE_TEMP_RECORD"]).read_text() == str(temp)
    roots_arg = next(
        x for x in workspace.calls()[0] if x.startswith("sandbox_workspace_write.writable_roots=")
    )
    assert str(temp) in json.loads(roots_arg.split("=", 1)[1])
    assert not temp.exists()
    assert state["status"] == "completed"
    delivery = workspace.run("collect", handle["task_id"])
    assert delivery["runtime_warnings"] == [
        f"New entry in allowed_write_root: {workspace.repo / 'smoke-test-123'}"
    ]


def test_configured_temp_root(workspace: Workspace, tmp_path: Path) -> None:
    temp_root = tmp_path / "custom" / "temp"
    record = tmp_path / "temp-record"
    workspace.env["FAKE_TEMP_RECORD"] = str(record)
    workspace.env["FAKE_ROOT"] = str(workspace.repo)
    workspace.settings.write_text(f'temp_root = "{temp_root}"\n' + workspace.settings.read_text())
    workspace.mode("temporary_and_root_entries")
    packet = json.dumps({**json.loads(PACKET), "allowed_writes": ["declared/output.txt"]})
    handle = workspace.submit(packet=packet, role="bounded-implementer")
    state = wait_for_terminal(workspace, handle["task_id"])
    temp = temp_root / handle["task_id"]
    assert record.read_text() == str(temp)
    roots_arg = next(
        x for x in workspace.calls()[0] if x.startswith("sandbox_workspace_write.writable_roots=")
    )
    assert str(temp) in json.loads(roots_arg.split("=", 1)[1])
    assert state["status"] == "completed"
    assert temp_root.is_dir() and not temp.exists()


def test_command_only_task_removes_temp_on_cancellation(
    workspace: Workspace, tmp_path: Path
) -> None:
    workspace.env["XDG_CACHE_HOME"] = str(tmp_path / "cache")
    record = tmp_path / "temp-record"
    workspace.env["FAKE_TEMP_RECORD"] = str(record)
    workspace.mode("temporary_hang")
    handle = workspace.submit(role="verification-runner")
    deadline = time.monotonic() + 10
    while not record.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    temp = Path(record.read_text())
    assert temp == tmp_path / "cache" / "tmp" / handle["task_id"]
    assert temp.is_dir()
    workspace.run("cancel", handle["task_id"])
    assert wait_for_terminal(workspace, handle["task_id"])["status"] == "cancelled"
    assert not temp.exists()


def test_submitting_returns_a_handle_without_waiting(workspace: Workspace) -> None:
    workspace.mode("silent_hang")

    handle = workspace.submit()

    assert handle["task_id"]
    assert handle["status"] in {"queued", "starting", "running"}
    assert (handle["model"], handle["effort"], handle["worker"]) == ("terra", "high", "codex")
    assert handle["project_root"] == str(workspace.repo)
    assert handle["next_action"] == "end_turn"
    assert handle["completion_delivery"] == "async_rewake"

    workspace.run("cancel", handle["task_id"])


def test_the_result_contract_is_written_beside_the_task_not_looked_up_on_disk(
    workspace: Workspace,
) -> None:
    handle = workspace.submit()
    state = wait_for_terminal(workspace, handle["task_id"])

    shipped = Path(state["task_dir"]) / "result.schema.json"
    assert shipped.exists(), "the worker must be handed a schema that travels with the task"
    assert str(shipped) in workspace.calls()[0]


def test_a_finished_task_is_delivered_once(workspace: Workspace) -> None:
    handle = workspace.submit()
    state = wait_for_terminal(workspace, handle["task_id"])
    assert state["status"] == "completed"

    envelope = workspace.run("collect", handle["task_id"])

    assert envelope["result"]["observed_facts"] == ["the fake worker ran"]
    assert envelope["status"] == "completed"

    again = workspace.run("collect", handle["task_id"], expect_success=False)
    assert again["error"] == "already_delivered"


def test_latest_codex_cumulative_usage_is_preserved_in_terminal_task_state(
    workspace: Workspace,
) -> None:
    workspace.mode("reported_usage")
    handle = workspace.submit()

    state = wait_for_terminal(workspace, handle["task_id"])

    assert state["status"] == "completed"
    assert state["provider_usage"] == {
        "source": "codex",
        "tokens": {
            "input_tokens": 200,
            "cached_input_tokens": 50,
            "cache_write_input_tokens": 12,
            "output_tokens": 40,
            "reasoning_output_tokens": 10,
        },
    }


def test_a_task_still_running_cannot_be_collected(workspace: Workspace) -> None:
    workspace.mode("silent_hang")
    handle = workspace.submit()

    refusal = workspace.run("collect", handle["task_id"], expect_success=False)

    assert refusal["error"] == "task_not_terminal"
    assert refusal["next_action"] == "end_turn"
    assert refusal["completion_delivery"] == "async_rewake"
    workspace.run("cancel", handle["task_id"])


def test_a_result_that_asks_for_a_decision_is_not_reported_as_completed(
    workspace: Workspace,
) -> None:
    workspace.mode("decision_needed")
    handle = workspace.submit()

    state = wait_for_terminal(workspace, handle["task_id"])

    assert state["status"] == "decision_needed"


def test_an_unverified_result_is_collected_without_being_reported_as_completed(
    workspace: Workspace,
) -> None:
    workspace.mode("unverified")
    handle = workspace.submit()

    state = wait_for_terminal(workspace, handle["task_id"])
    collected = workspace.run("collect", handle["task_id"])

    assert state["status"] == "unverified"
    assert collected["status"] == "unverified"
    assert collected["result"]["status"] == "unverified"


def test_writable_unverified_requires_finished_implementation(workspace: Workspace) -> None:
    packet = json.dumps({**json.loads(PACKET), "allowed_writes": ["allowed.txt"]})
    workspace.mode("unverified_incomplete")
    handle = workspace.submit(packet=packet, role="bounded-implementer")
    state = wait_for_terminal(workspace, handle["task_id"])
    assert state["status"] == "failed"
    assert state["failure_class"] == "incomplete_work"

    workspace.mode("unverified_complete")
    handle = workspace.submit(packet=packet, role="bounded-implementer")
    state = wait_for_terminal(workspace, handle["task_id"])
    assert state["status"] == "unverified"
    assert workspace.run("collect", handle["task_id"])["result"]["work_complete"] is True


def test_a_worker_that_exits_nonzero_is_a_failure(workspace: Workspace) -> None:
    workspace.mode("nonzero_exit")
    handle = workspace.submit()

    state = wait_for_terminal(workspace, handle["task_id"])

    assert state["status"] == "failed"
    assert state["terminal_reason"] == "nonzero_exit"


def test_opencode_expired_api_key_is_reported_on_collect(workspace: Workspace) -> None:
    workspace.settings.write_text(
        workspace.settings.read_text(encoding="utf-8").replace(
            'adapter = "codex"', 'adapter = "opencode"'
        ),
        encoding="utf-8",
    )
    workspace.mode("opencode_expired_api_key")
    handle = workspace.submit()

    state = wait_for_terminal(workspace, handle["task_id"])
    delivery = workspace.run("collect", handle["task_id"])

    assert state["failure_class"] == "auth_failed"
    assert delivery["failure_message"] == "The API key has expired. Create a new key and try again."


def test_a_backend_that_cannot_write_a_result_file_still_delivers_one(
    workspace: Workspace,
) -> None:
    # Only Codex can be handed an output path. OpenCode and claude return their
    # answer as the final message, so the result has to be taken from there.
    workspace.mode("result_in_message_only")
    handle = workspace.submit()

    state = wait_for_terminal(workspace, handle["task_id"])

    assert state["status"] == "completed"
    assert workspace.run("collect", handle["task_id"])["result"]["observed_facts"] == [
        "the fake worker ran"
    ]


def test_a_worker_that_writes_no_result_is_a_failure(workspace: Workspace) -> None:
    workspace.mode("empty_result")
    handle = workspace.submit()

    state = wait_for_terminal(workspace, handle["task_id"])

    assert state["status"] == "failed"
    assert state["terminal_reason"] == "empty_result"


def test_a_result_that_breaks_the_contract_is_a_failure_and_the_text_is_kept(
    workspace: Workspace,
) -> None:
    workspace.mode("invalid_result")
    handle = workspace.submit()

    state = wait_for_terminal(workspace, handle["task_id"])

    assert state["status"] == "failed"
    assert state["terminal_reason"] == "invalid_result"
    kept = Path(state["last_agent_message_path"]).read_text(encoding="utf-8")
    assert kept == "I did the thing"


def test_a_task_that_runs_out_of_time_records_where_it_stopped(workspace: Workspace) -> None:
    workspace.mode("tool_hang")
    handle = workspace.submit(role="impatient")

    state = wait_for_terminal(workspace, handle["task_id"])

    assert state["status"] == "timeout"
    assert state["failure_class"] == "tool_stall"


def test_a_worker_that_never_speaks_is_stopped_before_its_deadline(
    workspace: Workspace,
) -> None:
    workspace.mode("utterly_silent")
    handle = workspace.submit(role="impatient")

    state = wait_for_terminal(workspace, handle["task_id"])

    assert state["status"] == "timeout"
    assert state["failure_class"] == "wall_clock_timeout"
    assert state["duration_sec"] < 1.0


def test_a_worker_that_started_but_stalled_keeps_its_full_deadline(
    workspace: Workspace,
) -> None:
    workspace.mode("silent_hang")
    handle = workspace.submit(role="impatient")

    state = wait_for_terminal(workspace, handle["task_id"])

    assert state["status"] == "timeout"
    assert state["failure_class"] == "event_stream_stall"
    assert state["duration_sec"] >= 0.95


def test_a_usable_result_left_by_a_timed_out_worker_is_collected_as_degraded(
    workspace: Workspace,
) -> None:
    workspace.mode("finalization_hang")
    handle = workspace.submit(role="impatient")

    state = wait_for_terminal(workspace, handle["task_id"])

    assert state["status"] == "degraded"
    assert state["terminal_reason"] == "timeout_with_result"
    assert state["failure_class"] == "finalization_timeout"
    assert Path(state["recovered_result_path"]).exists(), "a usable final message is kept aside"


def test_the_salvaged_result_is_delivered_on_collect(workspace: Workspace) -> None:
    workspace.mode("finalization_hang")
    handle = workspace.submit(role="impatient")
    wait_for_terminal(workspace, handle["task_id"])

    delivery = workspace.run("collect", handle["task_id"])

    assert delivery["status"] == "degraded"
    assert delivery["result"]["observed_facts"] == ["the fake worker ran"]
    assert delivery["recovery_status"] == "usable"


def test_the_same_request_twice_while_it_is_running_reuses_the_first_task(
    workspace: Workspace,
) -> None:
    workspace.mode("silent_hang")
    first = workspace.submit()

    second = workspace.submit()

    assert second["task_id"] == first["task_id"]
    assert second["deduplicated"] is True

    workspace.run("cancel", first["task_id"])


def test_the_same_request_after_it_finished_is_asked_again(workspace: Workspace) -> None:
    first = workspace.submit()
    wait_for_terminal(workspace, first["task_id"])

    second = workspace.submit()

    assert second["task_id"] != first["task_id"]
    assert second["deduplicated"] is False


def test_host_only_work_never_starts_a_worker(workspace: Workspace) -> None:
    packet = json.dumps({**json.loads(PACKET), "host_only": True})

    refusal = workspace.submit(packet=packet)

    assert refusal["run_in"] == "claude-code"
    assert workspace.calls() == [], "no worker was started"


def test_a_role_that_must_declare_its_writes_is_refused_without_them(workspace: Workspace) -> None:
    refusal = workspace.submit(role="bounded-implementer")

    assert "allowed_writes" in refusal["error"]
    assert workspace.calls() == []


def test_a_cancelled_task_is_terminal_and_not_a_success(workspace: Workspace) -> None:
    workspace.mode("silent_hang")
    handle = workspace.submit()

    workspace.run("cancel", handle["task_id"])

    state = wait_for_terminal(workspace, handle["task_id"])
    assert state["status"] == "cancelled"


def test_a_worker_that_vanished_is_reclassified_rather_than_left_running(
    workspace: Workspace,
) -> None:
    workspace.mode("silent_hang")
    handle = workspace.submit()

    # Wait for the state, not for the lock. The supervisor takes the lock before it
    # records `running`, and a task killed in that gap is still inside the startup
    # grace that reconcile allows, so it is left alone rather than declared orphaned.
    while workspace.run("status", handle["task_id"])["status"] != "running":
        time.sleep(0.02)

    # Kill the worker the way a reboot would: no chance to record anything.
    os_pid = workspace.run("status", handle["task_id"])["pid"]
    os.killpg(os.getpgid(os_pid), signal.SIGKILL)
    time.sleep(0.2)

    reconciled = workspace.run("reconcile")

    assert reconciled["tasks"][0]["status"] == "orphaned"
    assert reconciled["ready"][0]["task_id"] == handle["task_id"]


def test_a_cancellation_stops_the_worker_without_signalling_the_supervisor(
    workspace: Workspace,
) -> None:
    # Cancel records the request and sends nothing. A signal can arrive before
    # the supervisor has a handler installed, which would kill it outright and
    # leave nobody to record the outcome.
    workspace.mode("silent_hang")
    handle = workspace.submit()

    workspace.run("cancel", handle["task_id"])

    assert wait_for_terminal(workspace, handle["task_id"])["status"] == "cancelled"


def test_incomplete_codex_session_resumes_to_completion(workspace: Workspace) -> None:
    workspace.mode("resume_success")
    packet = json.dumps({**json.loads(PACKET), "allowed_writes": ["allowed.txt"]})
    handle = workspace.submit(packet=packet, role="bounded-implementer")
    state = wait_for_terminal(workspace, handle["task_id"])
    calls = workspace.calls()
    assert state["status"] == "completed"
    assert state["resume_count"] == 1
    assert state["resume_results"][0]["result"]["work_complete"] is True
    assert calls[1][-3:] == ["resume", "session-1", "-"]
    assert (
        calls[0][calls[0].index("--output-schema") + 1]
        == (calls[1][calls[1].index("--output-schema") + 1])
    )
    assert "blockers" in (Path(state["task_dir"]) / "resume-prompt.md").read_text()


def test_resume_limit_stops_different_blockers(workspace: Workspace) -> None:
    workspace.settings.write_text(
        workspace.settings.read_text().replace(
            'adapter = "codex"', 'adapter = "codex"\nresume_limit = 1'
        )
    )
    workspace.mode("resume_limit")
    packet = json.dumps({**json.loads(PACKET), "allowed_writes": ["allowed.txt"]})
    handle = workspace.submit(packet=packet, role="bounded-implementer")
    state = wait_for_terminal(workspace, handle["task_id"])
    assert state["failure_class"] == "incomplete_work"
    assert state["resume_count"] == 1
    assert len(workspace.calls()) == 2
    assert state["resume_results"][0]["result"]["blockers"] == ["blocker-1"]


def test_repeated_blockers_stop_before_limit(workspace: Workspace) -> None:
    workspace.mode("resume_repeated")
    packet = json.dumps({**json.loads(PACKET), "allowed_writes": ["allowed.txt"]})
    handle = workspace.submit(packet=packet, role="bounded-implementer")
    state = wait_for_terminal(workspace, handle["task_id"])
    assert state["failure_class"] == "incomplete_work"
    assert state["resume_count"] == 1
    assert len(workspace.calls()) == 2


def test_read_only_incomplete_result_does_not_resume(workspace: Workspace) -> None:
    workspace.mode("unverified_incomplete")
    state = wait_for_terminal(workspace, workspace.submit()["task_id"])
    assert state["resume_count"] == 0
    assert len(workspace.calls()) == 1


def test_default_resume_limit_allows_two_attempts(workspace: Workspace) -> None:
    workspace.mode("resume_limit")
    packet = json.dumps({**json.loads(PACKET), "allowed_writes": ["allowed.txt"]})
    handle = workspace.submit(packet=packet, role="bounded-implementer")
    state = wait_for_terminal(workspace, handle["task_id"])
    assert state["failure_class"] == "incomplete_work"
    assert state["resume_count"] == 2
    assert [result["result"]["blockers"] for result in state["resume_results"]] == [
        ["blocker-1"],
        ["blocker-2"],
    ]
    assert len(workspace.calls()) == 3
