"""Which worker a submit goes to: a hashed comparison arm, and no sends to a broken one.

Most cases call ``tasks.submit`` in process with the worker launch stubbed out, and
write the history they need into the task root as an earlier run would have left it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from conftest import PACKET, SETTINGS, Workspace
from delegate import config, store, tasks
from test_lifecycle import wait_for_terminal

COMPARE = """
[workers.opencode]
adapter = "opencode"
enabled = true
standard = { model = "sonnet", effort = "high" }

[roles.ab]
level = "standard"
compare_workers = ["codex", "opencode"]
"""
ARMS = ("codex", "opencode")
NOW = datetime.now().astimezone().replace(microsecond=0)


class _Process:
    pid = 123


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(shutil, "which", lambda name, *args, **kwargs: f"/usr/bin/{name}")
    monkeypatch.setattr(tasks.subprocess, "Popen", lambda *args, **kwargs: _Process())
    monkeypatch.setenv("DELEGATE_WATCH_ROOT", str(tmp_path / "watch-sessions"))
    (tmp_path / "tests").mkdir()
    return tmp_path


def submit(project: Path, role: str = "ab", title: str = "t", **kwargs: Any) -> dict[str, Any]:
    return tasks.submit(
        project_root=project,
        settings=config.parse((SETTINGS + COMPARE).encode(), Path("delegate.toml")),
        role=role,
        title=title,
        value=json.loads(PACKET),
        **kwargs,
    )


def state_of(project: Path, handle: dict[str, Any]) -> dict[str, Any]:
    return store.read_json(Path(handle["task_dir"]) / "state.json")


def force_arm(monkeypatch: pytest.MonkeyPatch, arm: str) -> None:
    monkeypatch.setattr(tasks, "arm_for", lambda task_id, arms: arm)


def history(project: Path, sequence: str, worker: str = "codex") -> list[str]:
    """Earlier tasks, oldest first, one per character: F fails with auth_failed, L with
    launch_error, . completes, o fails with auth_failed on the other worker."""
    ids = []
    for index, mark in enumerate(sequence):
        task_id = f"old-{index}"
        failing = mark in "FLo"
        store.write_json(
            tasks.task_root(project) / task_id / "state.json",
            {
                "task_id": task_id,
                "worker": "opencode" if mark == "o" else worker,
                "status": "failed" if failing else "completed",
                "failure_class": "auth_failed" if mark in "Fo" else "launch_error",
                "terminal_reason": "launch_error" if mark == "L" else "nonzero_exit",
                "terminal_at": (NOW - timedelta(minutes=len(sequence) - index)).isoformat(),
            },
        )
        ids.append(task_id)
    return ids


def test_the_arm_is_a_stable_hash_of_the_task_id_and_both_arms_occur() -> None:
    ids = [str(uuid.uuid4()) for _ in range(40)]
    arms = [tasks.arm_for(task_id, ARMS) for task_id in ids]
    assert arms == [tasks.arm_for(task_id, ARMS) for task_id in ids]
    assert set(arms) == set(ARMS)


def test_a_comparing_role_runs_the_arm_its_task_id_hashes_to(project: Path) -> None:
    for index in range(4):
        handle = submit(project, title=f"t{index}")
        state = state_of(project, handle)
        assert state["worker"] == state["route_arm"] == tasks.arm_for(handle["task_id"], ARMS)
        assert state["route_policy"] == "compare"


def test_an_explicit_worker_wins_and_a_plain_role_records_no_route(project: Path) -> None:
    forced = state_of(project, submit(project, worker="opencode"))
    assert (forced["worker"], forced["route_policy"], forced["route_arm"]) == (
        "opencode",
        "explicit",
        None,
    )
    plain = state_of(project, submit(project, role="artifact-auditor"))
    assert (plain["worker"], plain["route_policy"], plain["route_arm"]) == ("codex", None, None)


def test_identical_running_requests_merge_whichever_arm_the_new_id_hashes_to(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_arm(monkeypatch, "codex")
    first = submit(project)
    for arm in ARMS:
        force_arm(monkeypatch, arm)
        again = submit(project)
        assert again["task_id"] == first["task_id"] and again["deduplicated"] is True


def test_an_arm_that_cannot_start_hands_over_to_the_other(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_arm(monkeypatch, "opencode")
    monkeypatch.setattr(
        shutil, "which", lambda name, *args, **kwargs: None if name == "opencode" else name
    )
    state = state_of(project, submit(project))
    assert (state["worker"], state["route_arm"]) == ("codex", "codex")

    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: None)
    with pytest.raises(tasks.SubmitRefused, match="not on PATH: opencode"):
        submit(project, title="nothing runs")


@pytest.mark.parametrize(
    "value",
    ['["codex"]', '["codex", "codex"]', '["codex", "opencode", "codex"]', '["codex", "nobody"]'],
)
def test_compare_workers_must_name_exactly_two_known_distinct_workers(value: str) -> None:
    body = COMPARE.replace('["codex", "opencode"]', value)
    with pytest.raises(config.ConfigError, match="compare_workers"):
        config.parse((SETTINGS + body).encode(), Path("delegate.toml"))


def test_a_disabled_arm_is_rejected_when_the_config_loads() -> None:
    body = COMPARE.replace("enabled = true", "enabled = false")
    with pytest.raises(config.ConfigError, match="not enabled: opencode"):
        config.parse((SETTINGS + body).encode(), Path("delegate.toml"))


# F: auth_failed on codex, L: launch_error, .: completed, o: auth_failed on another worker.
@pytest.mark.parametrize(
    ("sequence", "refused"),
    [
        ("FFF", True),
        (".FFF", True),
        ("FFF.", False),
        ("FF.F", False),
        ("FFL", False),
        ("FF", False),
        ("FFoF", True),
        ("oFF", False),
    ],
)
def test_a_worker_whose_last_three_tasks_failed_alike_is_refused(
    project: Path, sequence: str, refused: bool
) -> None:
    ids = history(project, sequence)
    if not refused:
        assert submit(project, role="artifact-auditor")["status"] == "starting"
        return
    with pytest.raises(tasks.WorkerUnhealthy) as error:
        submit(project, role="artifact-auditor")
    detail = error.value.detail
    assert (detail["worker"], detail["failure_class"]) == ("codex", "auth_failed")
    assert set(detail["task_ids"]) <= set(ids) and len(detail["task_ids"]) == 3
    assert "--allow-unhealthy" in str(error.value)


@pytest.mark.parametrize(
    ("fields", "fault"),
    [
        ({"failure_class": "auth_failed"}, "auth_failed"),
        ({"failure_class": "KeyError", "terminal_reason": "supervisor_error"}, "supervisor_error"),
        ({"failure_class": "OSError", "terminal_reason": "launch_error"}, "launch_error"),
        ({"failure_class": "worker_api_error"}, None),
        ({"failure_class": "provider_capacity"}, None),
        ({"failure_class": "auth_failed", "status": "timeout"}, None),
        ({"failure_class": "auth_failed", "status": "completed"}, None),
    ],
)
def test_which_failures_say_the_worker_is_broken(fields: dict[str, str], fault: str | None) -> None:
    state = {"status": "failed", "terminal_reason": "nonzero_exit", **fields}
    assert tasks.worker_fault(state) == fault


def test_allow_unhealthy_submits_anyway_and_records_it(project: Path) -> None:
    ids = history(project, "FFF")
    state = state_of(project, submit(project, role="artifact-auditor", allow_unhealthy=True))
    assert state["unhealthy_override"] == {"failure_class": "auth_failed", "task_ids": ids[::-1]}


def test_an_explicit_worker_is_refused_not_swapped_even_in_a_comparing_role(project: Path) -> None:
    history(project, "FFF", worker="opencode")
    with pytest.raises(tasks.WorkerUnhealthy):
        submit(project, worker="opencode")


@pytest.mark.parametrize("chosen", ARMS)
def test_a_comparison_moves_to_the_other_arm_only_when_that_one_is_healthy(
    project: Path, monkeypatch: pytest.MonkeyPatch, chosen: str
) -> None:
    force_arm(monkeypatch, chosen)
    history(project, "FFF", worker="codex")
    state = state_of(project, submit(project))
    assert (state["worker"], state["route_arm"]) == ("opencode", "opencode")

    # With both broken there is nowhere to go, and --allow-unhealthy keeps the chosen arm.
    store_old = tasks.task_root(project) / "old-0"
    for index in range(3):
        task = store.read_json(store_old / "state.json") | {
            "task_id": f"oc-{index}",
            "worker": "opencode",
        }
        store.write_json(tasks.task_root(project) / f"oc-{index}" / "state.json", task)
    with pytest.raises(tasks.WorkerUnhealthy) as error:
        submit(project, title="again")
    assert error.value.detail["worker"] == chosen
    kept = state_of(project, submit(project, title="again", allow_unhealthy=True))
    assert kept["worker"] == chosen


def test_an_identical_running_task_is_returned_rather_than_refused(project: Path) -> None:
    running = submit(project, role="artifact-auditor")
    history(project, "FFF")
    again = submit(project, role="artifact-auditor")
    assert (again["task_id"], again["deduplicated"]) == (running["task_id"], True)
    with pytest.raises(tasks.WorkerUnhealthy):
        submit(project, role="artifact-auditor", title="something else")


def test_real_expired_key_failures_stop_the_fourth_submit(workspace: Workspace) -> None:
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

    (workspace.repo / "packet.json").write_text(PACKET, encoding="utf-8")
    refused = subprocess.run(
        [sys.executable, "-m", "delegate", "submit", "--role", "artifact-auditor"]
        + ["--title", "x", "--packet", "packet.json"],
        capture_output=True,
        text=True,
        env=workspace.env,
        cwd=workspace.repo,
    )
    out = json.loads(refused.stdout)
    assert (refused.returncode, out["error"]) == (7, "worker_unhealthy")
    assert (out["worker"], out["failure_class"]) == ("codex", "auth_failed")
    assert sorted(out["task_ids"]) == sorted(done)
    assert "--allow-unhealthy" in out["reason"]

    assert workspace.submit("--allow-unhealthy", title="deliberate", expect_success=True)
