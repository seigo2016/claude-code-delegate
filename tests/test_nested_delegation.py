"""A worker is not the Chair: it must not start workers of its own."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import PACKET, Workspace
from delegate import config, runner, tasks
from test_lifecycle import wait_for_terminal

NESTED_SUBMIT = 6


def test_the_worker_environment_carries_the_marker_and_its_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DELEGATE_PARENT_TASK", "stale")

    env = runner.worker_environment("task-1")

    assert env["DELEGATE_WORKER"] == "1"
    assert env["DELEGATE_PARENT_TASK"] == "task-1"


def test_a_running_worker_sees_the_marker(workspace: Workspace, tmp_path: Path) -> None:
    record = tmp_path / "env-record"
    workspace.env["FAKE_ENV_RECORD"] = str(record)
    handle = workspace.submit()
    wait_for_terminal(workspace, handle["task_id"])

    assert json.loads(record.read_text()) == {
        "DELEGATE_WORKER": "1",
        "DELEGATE_PARENT_TASK": handle["task_id"],
    }


def test_submit_is_refused_under_the_marker_and_creates_nothing(workspace: Workspace) -> None:
    workspace.env["DELEGATE_WORKER"] = "1"
    (workspace.repo / "packet.json").write_text(PACKET, encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "delegate",
            "submit",
            "--role",
            "artifact-auditor",
            "--title",
            "nested",
            "--packet",
            "packet.json",
        ],
        capture_output=True,
        text=True,
        env=workspace.env,
        cwd=workspace.repo,
    )

    assert completed.returncode == NESTED_SUBMIT
    assert json.loads(completed.stdout)["error"] == "nested_submit"
    assert not (workspace.repo / ".claude" / "logs").exists()


def test_the_prompt_tells_the_worker_it_is_not_the_chair() -> None:
    role = config.Role(name="artifact-auditor", level="standard")
    plan = config.Plan(
        role=role, worker="codex", adapter="codex", model="m", effort="high", timeout=60
    )
    prompt = tasks.compose_prompt(
        Path("/repo"),
        plan,
        {
            "objective": "Check something.",
            "read": [],
            "allowed_writes": [],
            "required_evidence": ["a count"],
            "host_only": False,
        },
    )

    assert "not the Chair" in prompt
    assert "must not delegate to other workers" in prompt
    assert "whatever repository instructions say" in prompt
