"""A configuration file and a task ledger that live apart from the project root."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from conftest import SETTINGS, Workspace
from test_lifecycle import wait_for_terminal

WAKE = 2


def alternate_config(tmp_path: Path) -> Path:
    path = tmp_path / "elsewhere" / "delegate.toml"
    path.parent.mkdir()
    path.write_text(SETTINGS + '\n[roles.only-here]\nlevel = "light"\n', encoding="utf-8")
    return path


def test_default_paths_are_unchanged_and_record_the_config(workspace: Workspace) -> None:
    handle = workspace.submit()
    state = wait_for_terminal(workspace, handle["task_id"])

    assert (workspace.repo / ".claude" / "logs" / "delegate" / handle["task_id"]).is_dir()
    assert "task_root" not in handle
    assert state["config_path"] == str(workspace.settings)
    assert state["config_sha256"] == hashlib.sha256(workspace.settings.read_bytes()).hexdigest()
    assert workspace.run("collect", handle["task_id"])["status"] == "completed"


def test_a_task_with_config_and_task_root_is_stored_and_collected_there(
    workspace: Workspace, tmp_path: Path
) -> None:
    config_path = alternate_config(tmp_path)
    ledger = tmp_path / "ledger"
    # The default settings no longer know the role, so only --config can supply it.
    assert "only-here" not in workspace.settings.read_text()

    handle = workspace.submit(
        "--config", str(config_path), "--task-root", str(ledger), role="only-here"
    )
    assert handle["task_root"] == str(ledger)
    assert (ledger / handle["task_id"] / "state.json").exists()
    assert not (workspace.repo / ".claude" / "logs").exists()

    root = ("--task-root", str(ledger))
    state = wait_for_terminal_at(workspace, handle["task_id"], root)
    assert state["status"] == "completed"
    assert state["config_path"] == str(config_path)
    assert state["config_sha256"] == hashlib.sha256(config_path.read_bytes()).hexdigest()
    assert state["task_root"] == str(ledger)
    # The worker still runs inside the project, not beside the ledger or the config.
    call = workspace.calls()[0]
    assert call[call.index("-C") + 1] == str(workspace.repo)

    listing = workspace.run("reconcile", *root)
    assert [task["task_id"] for task in listing["ready"]] == [handle["task_id"]]
    delivery = workspace.run("collect", handle["task_id"], *root)
    assert delivery["status"] == "completed"
    assert workspace.run("collect", handle["task_id"], *root, expect_success=False)["error"] == (
        "already_delivered"
    )
    # Without the option the task is not in the default ledger.
    assert (
        "task_not_found"
        in workspace.run("status", handle["task_id"], expect_success=False)["error"]
    )


def wait_for_terminal_at(workspace: Workspace, task_id: str, root: tuple[str, str]) -> dict:
    import time

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        state = workspace.run("status", task_id, *root)
        if state["status"] not in {"queued", "starting", "running", "cancellation_requested"}:
            return state
        time.sleep(0.05)
    raise AssertionError("task never finished")


def test_cancel_and_watch_find_a_task_under_another_task_root(
    workspace: Workspace, tmp_path: Path
) -> None:
    ledger = tmp_path / "ledger"
    root = ("--task-root", str(ledger))
    workspace.mode("utterly_silent")
    handle = workspace.submit("--task-root", str(ledger))
    cancelled = workspace.run("cancel", handle["task_id"], *root)
    assert cancelled["status"] in {"cancellation_requested", "cancelled"}
    state = wait_for_terminal_at(workspace, handle["task_id"], root)
    assert state["status"] == "cancelled"

    watched = subprocess.run(
        [sys.executable, "-m", "delegate", "watch", *root],
        capture_output=True,
        text=True,
        env={**workspace.env, "DELEGATE_WATCH_SEC": "5"},
        cwd=workspace.repo,
    )
    assert watched.returncode == WAKE
    assert json.loads(watched.stdout)["ready"][0]["task_id"] == handle["task_id"]


def test_the_session_hook_finds_a_task_under_another_task_root(
    workspace: Workspace, tmp_path: Path
) -> None:
    workspace.env["CLAUDE_CODE_SESSION_ID"] = "session-1"
    ledger = tmp_path / "ledger"
    handle = workspace.submit("--task-root", str(ledger))
    assert handle["task_root"] == str(ledger)

    def hook(stdout: str) -> subprocess.CompletedProcess[str]:
        hook_input = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "session_id": "session-1",
            "tool_response": {"stdout": stdout},
        }
        return subprocess.run(
            [sys.executable, "-m", "delegate", "_hook-watch"],
            input=json.dumps(hook_input),
            capture_output=True,
            text=True,
            env={**workspace.env, "DELEGATE_WATCH_SEC": "20"},
            cwd=tmp_path,
        )

    # The handle was hidden by a pipeline: only the pending registry can find it.
    result = hook("nothing useful")
    assert result.returncode == WAKE
    assert [item["task_id"] for item in json.loads(result.stdout)["ready"]] == [handle["task_id"]]

    # And with the handle in the output, the group is remembered inside the ledger.
    second = workspace.submit("--task-root", str(ledger), title="again")
    result = hook(json.dumps(second))
    assert result.returncode == WAKE
    group = json.loads((ledger / second["task_id"] / "watch-group.json").read_text())
    assert group["watch_tasks"][0]["task_root"] == str(ledger)
    wait_for_terminal_at(workspace, second["task_id"], ("--task-root", str(ledger)))
    delivery = workspace.run("collect", second["task_id"], "--task-root", str(ledger))
    assert delivery["status"] == "completed"


def run_hook(
    workspace: Workspace, name: str, hook_input: dict, *args: str, cwd: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "delegate", name, *args],
        input=json.dumps(hook_input),
        capture_output=True,
        text=True,
        env={**workspace.env, "DELEGATE_WATCH_SEC": "20"},
        cwd=cwd,
    )


def test_a_session_task_under_another_task_root_is_followed_by_both_hooks(
    workspace: Workspace, tmp_path: Path
) -> None:
    workspace.env["CLAUDE_CODE_SESSION_ID"] = "session-1"
    ledger = tmp_path / "ledger"
    handle = workspace.submit("--task-root", str(ledger), expect_success=True)

    # The wake notification names the task root and says to pass it.
    woken = run_hook(
        workspace,
        "_hook-watch",
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "session_id": "session-1",
            "tool_response": {"stdout": json.dumps(handle)},
        },
        cwd=tmp_path,
    )
    assert woken.returncode == WAKE
    notice = json.loads(woken.stdout)
    assert [(line["task_id"], line["task_root"]) for line in notice["ready"]] == [
        (handle["task_id"], str(ledger))
    ]
    assert "--task-root" in notice["next_action"]

    # A later session start finds the same task, though it is not in the default ledger.
    started = run_hook(
        workspace,
        "_hook-reconcile",
        {"session_id": "session-1"},
        "--project-root",
        str(workspace.repo),
        cwd=tmp_path,
    )
    assert started.returncode == 0
    listing = json.loads(started.stdout)
    assert [line["task_id"] for line in listing["tasks"]] == [handle["task_id"]]
    assert "--task-root" in listing["next_action"]

    # And the printed values are enough to collect it.
    line = listing["ready"][0]
    delivery = workspace.run(
        "collect",
        line["task_id"],
        "--project-root",
        line["project_root"],
        "--task-root",
        line["task_root"],
    )
    assert delivery["status"] == "completed"

    other = run_hook(
        workspace,
        "_hook-reconcile",
        {"session_id": "session-2"},
        "--project-root",
        str(workspace.repo),
        cwd=tmp_path,
    )
    assert json.loads(other.stdout)["tasks"] == []


def test_default_ledger_lines_do_not_mention_a_task_root(workspace: Workspace) -> None:
    workspace.env["CLAUDE_CODE_SESSION_ID"] = "session-1"
    handle = workspace.submit(expect_success=True)
    wait_for_terminal(workspace, handle["task_id"])
    listing = workspace.run("reconcile")
    assert "task_root" not in listing["ready"][0]
    assert listing["next_action"] == "collect each ready task once"
