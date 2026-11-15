"""Who may collect or cancel a task.

A session task stays open to any session, as it always was. A bridge task, one
submitted with an explicit owner, answers only to that owner, and bridge tasks
are never announced to a Claude Code session.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from conftest import Workspace
from delegate import store
from test_lifecycle import wait_for_terminal
from test_watch import QUIET, hook_watch

OWNER_MISMATCH = 5


def call(workspace: Workspace, *args: str, env: dict[str, str] | None = None) -> tuple[int, dict]:
    completed = subprocess.run(
        [sys.executable, "-m", "delegate", *args],
        capture_output=True,
        text=True,
        env={**workspace.env, **(env or {})},
        cwd=workspace.repo,
    )
    return completed.returncode, json.loads(completed.stdout or "{}")


def finished(workspace: Workspace, *extra: str, title: str = "t") -> str:
    handle = workspace.submit(*extra, title=title)
    wait_for_terminal(workspace, handle["task_id"])
    return handle["task_id"]


def state_of(workspace: Workspace, task_id: str) -> dict:
    return workspace.run("status", task_id)


def test_a_plain_submit_is_session_owned(workspace: Workspace) -> None:
    workspace.env["CLAUDE_CODE_SESSION_ID"] = "s1"
    state = state_of(workspace, finished(workspace))
    assert (state["owner_kind"], state["owner_id"]) == ("session", "s1")
    assert state["submitter_session_id"] == "s1"


def test_an_explicit_owner_makes_a_bridge_task_with_no_session(workspace: Workspace) -> None:
    workspace.env["CLAUDE_CODE_SESSION_ID"] = "s1"
    task_id = finished(workspace, "--owner", "bridge-a")
    state = state_of(workspace, task_id)
    assert (state["owner_kind"], state["owner_id"]) == ("bridge", "bridge-a")
    assert state["submitter_session_id"] is None


def test_the_worker_environment_lacks_the_submitting_session(monkeypatch) -> None:
    from delegate import runner

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s1")
    monkeypatch.setenv("KEEP_ME", "1")
    env = runner.worker_environment()
    assert "CLAUDE_CODE_SESSION_ID" not in env
    assert env["KEEP_ME"] == "1"


def test_a_bridge_task_is_collected_only_by_its_owner(workspace: Workspace) -> None:
    task_id = finished(workspace, "--owner", "bridge-a")

    for env in ({}, {"CLAUDE_CODE_SESSION_ID": "s1"}):
        code, out = call(workspace, "collect", task_id, env=env)
        assert (code, out["error"]) == (OWNER_MISMATCH, "owner_mismatch")
    code, out = call(workspace, "collect", task_id, "--owner", "bridge-b")
    assert code == OWNER_MISMATCH
    assert not state_of(workspace, task_id)["delivered_at"]

    code, out = call(workspace, "collect", task_id, "--owner", "bridge-a")
    assert code == 0
    state = state_of(workspace, task_id)
    assert state["delivered_to"] == {"kind": "bridge", "id": "bridge-a"}

    code, out = call(workspace, "collect", task_id, "--owner", "bridge-a")
    assert (code, out["error"]) == (3, "already_delivered")


def test_a_session_task_is_open_to_any_session_but_not_to_a_bridge(workspace: Workspace) -> None:
    workspace.env["CLAUDE_CODE_SESSION_ID"] = "s1"
    task_id = finished(workspace)

    code, out = call(workspace, "collect", task_id, "--owner", "bridge-a")
    assert (code, out["error"]) == (OWNER_MISMATCH, "owner_mismatch")

    code, _ = call(workspace, "collect", task_id, env={"CLAUDE_CODE_SESSION_ID": "s2"})
    assert code == 0
    assert state_of(workspace, task_id)["delivered_to"] == {"kind": "session", "id": "s2"}
    # A session caller without an id may collect too, and is recorded as null.
    other = finished(workspace, title="other")
    assert call(workspace, "collect", other, env={"CLAUDE_CODE_SESSION_ID": ""})[0] == 0
    assert state_of(workspace, other)["delivered_to"] == {"kind": "session", "id": None}


def test_legacy_state_without_owner_fields_is_session_owned(workspace: Workspace) -> None:
    task_id = finished(workspace)
    path = Path(state_of(workspace, task_id)["state_path"])
    legacy = {k: v for k, v in store.read_json(path).items() if not k.startswith("owner_")}
    store.write_json(path, legacy)

    assert call(workspace, "collect", task_id, "--owner", "bridge-a")[0] == OWNER_MISMATCH
    assert call(workspace, "status", task_id)[0] == 0
    assert call(workspace, "reconcile")[0] == 0
    assert call(workspace, "collect", task_id, env={"CLAUDE_CODE_SESSION_ID": "s9"})[0] == 0


def test_cancel_follows_the_same_owner_rule(workspace: Workspace) -> None:
    workspace.mode("silent_hang")
    handle = workspace.submit("--owner", "bridge-a")
    task_id = handle["task_id"]

    code, out = call(workspace, "cancel", task_id)
    assert (code, out["error"]) == (OWNER_MISMATCH, "owner_mismatch")
    assert call(workspace, "cancel", task_id, "--owner", "bridge-b")[0] == OWNER_MISMATCH
    assert state_of(workspace, task_id)["status"] not in {"cancellation_requested", "cancelled"}

    assert call(workspace, "cancel", task_id, "--owner", "bridge-a")[0] == 0
    assert wait_for_terminal(workspace, task_id)["status"] == "cancelled"

    workspace.mode("silent_hang")
    session_task = workspace.submit(title="session")["task_id"]
    assert call(workspace, "cancel", session_task, "--owner", "bridge-a")[0] == OWNER_MISMATCH
    assert call(workspace, "cancel", session_task)[0] == 0
    wait_for_terminal(workspace, session_task)


def test_identical_submissions_are_not_deduplicated_across_owner_kinds(
    workspace: Workspace,
) -> None:
    workspace.mode("silent_hang")
    workspace.env["CLAUDE_CODE_SESSION_ID"] = "s1"
    session = workspace.submit()
    bridge_a = workspace.submit("--owner", "bridge-a")
    bridge_b = workspace.submit("--owner", "bridge-b")
    bridge_a_again = workspace.submit("--owner", "bridge-a")

    assert len({session["task_id"], bridge_a["task_id"], bridge_b["task_id"]}) == 3
    assert bridge_a_again["task_id"] == bridge_a["task_id"]
    assert bridge_a_again["deduplicated"] is True
    for handle, owner in ((session, None), (bridge_a, "bridge-a"), (bridge_b, "bridge-b")):
        call(workspace, "cancel", handle["task_id"], *(["--owner", owner] if owner else []))


def test_bridge_tasks_are_never_announced_to_a_session(
    workspace: Workspace, tmp_path: Path
) -> None:
    workspace.env["CLAUDE_CODE_SESSION_ID"] = "session-1"
    handle = workspace.submit("--owner", "bridge-a")
    wait_for_terminal(workspace, handle["task_id"])

    hooked = hook_watch(workspace, json.dumps(handle), cwd=tmp_path, session_id="session-1")
    assert hooked.returncode == QUIET
    assert json.loads(hooked.stdout)["ready"] == []

    reconciled = subprocess.run(
        [
            sys.executable,
            "-m",
            "delegate",
            "_hook-reconcile",
            "--project-root",
            str(workspace.repo),
        ],
        input=json.dumps({"session_id": "session-1"}),
        capture_output=True,
        text=True,
        env=workspace.env,
        cwd=tmp_path,
    )
    assert reconciled.returncode == 0
    assert reconciled.stdout == ""


def test_manual_reconcile_offers_a_session_only_session_tasks(workspace: Workspace) -> None:
    session_id = finished(workspace, title="mine")
    bridge_id = finished(workspace, "--owner", "bridge-a", title="theirs")

    reconciled = workspace.run("reconcile")

    assert [line["task_id"] for line in reconciled["ready"]] == [session_id]
    assert bridge_id not in {line["task_id"] for line in reconciled["tasks"]}


def test_manual_reconcile_with_an_owner_shows_only_that_owners_tasks(
    workspace: Workspace,
) -> None:
    finished(workspace, title="mine")
    mine = finished(workspace, "--owner", "bridge-a", title="theirs")
    finished(workspace, "--owner", "bridge-b", title="other")

    reconciled = workspace.run("reconcile", "--owner", "bridge-a")

    assert [line["task_id"] for line in reconciled["ready"]] == [mine]
    assert [line["task_id"] for line in reconciled["tasks"]] == [mine]


def test_manual_watch_is_not_woken_by_the_other_kind(workspace: Workspace) -> None:
    bridge_id = finished(workspace, "--owner", "bridge-a", title="theirs")
    env = {"DELEGATE_WATCH_SEC": "1"}

    code, out = call(workspace, "watch", env=env)
    assert (code, out["ready"]) == (QUIET, [])

    code, out = call(workspace, "watch", "--owner", "bridge-a", env=env)
    assert code == 2
    assert [line["task_id"] for line in out["ready"]] == [bridge_id]

    session_id = finished(workspace, title="mine")
    code, out = call(workspace, "watch", "--owner", "bridge-a", env=env)
    assert [line["task_id"] for line in out["ready"]] == [bridge_id]
    code, out = call(workspace, "watch", env=env)
    assert [line["task_id"] for line in out["ready"]] == [session_id]
