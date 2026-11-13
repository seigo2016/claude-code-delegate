"""Do not keep sending work to a worker that is broken rather than the task.

Every case has its own task root and builds its history with explicit timestamps,
measured back from one fixed ``NOW``. Nothing here depends on how long a real task
takes, or on a real task finishing after the history it is compared with.
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from conftest import PACKET, SETTINGS, Workspace
from delegate import config, store, tasks
from test_compare_workers import COMPARE

WORKER_UNHEALTHY = 7
NOW = datetime.now().astimezone().replace(microsecond=0)
COOLDOWN = timedelta(hours=6)


def stamp(ago: timedelta) -> str:
    return (NOW - ago).isoformat(timespec="seconds")


@dataclass
class Case:
    workspace: Workspace
    ledger: Path

    @property
    def root(self) -> tuple[str, str]:
        return ("--task-root", str(self.ledger))

    def record(
        self,
        task_id: str,
        *,
        worker: str = "codex",
        role: str = "artifact-auditor",
        status: str = "failed",
        failure_class: str | None = "auth_failed",
        terminal_reason: str = "nonzero_exit",
        ago: timedelta = timedelta(minutes=30),
        **fields: Any,
    ) -> str:
        """Put a finished task into this case's ledger as an earlier run would have left it."""
        store.write_json(
            self.ledger / task_id / "state.json",
            {
                "task_id": task_id,
                "role": role,
                "worker": worker,
                "status": status,
                "failure_class": failure_class,
                "terminal_reason": terminal_reason,
                "created_at": stamp(ago),
                "terminal_at": stamp(ago),
                **fields,
            },
        )
        return task_id

    def failures(
        self, count: int = 3, *, ago: timedelta = timedelta(minutes=30), **fields: Any
    ) -> list[str]:
        """``count`` failures, the newest ``ago`` ago and each earlier one a minute before."""
        return [
            self.record(f"old-{i}", ago=ago + timedelta(minutes=count - 1 - i), **fields)
            for i in range(count)
        ]

    def submit(self, *extra: str, role: str = "artifact-auditor", **kwargs: Any) -> dict:
        return self.workspace.submit(*self.root, *extra, role=role, **kwargs)

    def refused(self, *extra: str, **kwargs: Any) -> dict:
        out = self.submit(*extra, **kwargs)
        assert "task_id" not in out, out
        return out

    def state(self, handle: dict) -> dict:
        return self.workspace.run("status", handle["task_id"], *self.root)

    def exit_code(self, *extra: str, role: str = "artifact-auditor") -> int:
        (self.workspace.repo / "packet.json").write_text(PACKET, encoding="utf-8")
        return subprocess.run(
            [sys.executable, "-m", "delegate", "submit", "--role", role, "--title", "x"]
            + ["--packet", "packet.json", *self.root, *extra],
            capture_output=True,
            text=True,
            env=self.workspace.env,
            cwd=self.workspace.repo,
        ).returncode

    def wait(self, handle: dict) -> dict:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            state = self.state(handle)
            if state["status"] not in {"queued", "starting", "running", "cancellation_requested"}:
                return state
            time.sleep(0.05)
        raise AssertionError(f"task {handle['task_id']} never finished")


@pytest.fixture
def case(workspace: Workspace, tmp_path: Path) -> Case:
    # Real submits that must not finish would otherwise add their own history.
    workspace.mode("utterly_silent")
    return Case(workspace, tmp_path / "ledger")


def use_compare(case: Case) -> None:
    case.workspace.settings.write_text(SETTINGS + COMPARE, encoding="utf-8")


def test_three_auth_failures_in_a_row_refuse_the_worker(case: Case) -> None:
    ids = case.failures()

    out = case.refused()

    assert out["error"] == "worker_unhealthy"
    assert (out["worker"], out["failure_class"]) == ("codex", "auth_failed")
    assert out["task_ids"] == ["old-2", "old-1", "old-0"]
    assert set(out["task_ids"]) == set(ids)
    assert case.exit_code() == WORKER_UNHEALTHY
    assert sorted(p.name for p in case.ledger.iterdir() if p.is_dir()) == sorted(ids)


def test_an_expired_key_seen_three_times_stops_the_fourth_submit(case: Case) -> None:
    workspace = case.workspace
    workspace.settings.write_text(
        workspace.settings.read_text(encoding="utf-8").replace(
            'adapter = "codex"', 'adapter = "opencode"'
        ),
        encoding="utf-8",
    )
    workspace.mode("opencode_expired_api_key")
    done = []
    for index in range(3):
        handle = case.submit(title=f"try-{index}", expect_success=True)
        assert case.wait(handle)["failure_class"] == "auth_failed"
        done.append(handle["task_id"])

    out = case.refused()
    assert out["error"] == "worker_unhealthy"
    assert sorted(out["task_ids"]) == sorted(done)

    handle = case.submit("--allow-unhealthy", title="deliberate", expect_success=True)
    assert case.wait(handle)["failure_class"] == "auth_failed"


def test_launch_and_supervisor_errors_count_as_the_worker_being_broken(case: Case) -> None:
    case.failures(failure_class="launch_error", terminal_reason="launch_error")
    assert case.refused()["failure_class"] == "launch_error"


def test_supervisor_errors_group_whatever_exception_named_them(case: Case) -> None:
    for index, name in enumerate(["KeyError", "ValueError", "OSError"]):
        case.record(
            f"sup-{index}",
            failure_class=name,
            terminal_reason="supervisor_error",
            ago=timedelta(minutes=30 - index),
        )

    assert case.refused()["failure_class"] == "supervisor_error"


@pytest.mark.parametrize("failure_class", ["auth_failed", "auth_error", "api_key_expired"])
def test_every_credential_class_counts(case: Case, failure_class: str) -> None:
    case.failures(failure_class=failure_class)
    assert case.refused()["failure_class"] == failure_class


@pytest.mark.parametrize(
    "failure_class",
    [
        "nonzero_exit",
        "worker_api_error",
        "provider_capacity",
        "incomplete_work",
        "empty_result",
        "invalid_result",
        "write_scope_violation",
    ],
)
def test_a_failure_that_may_be_the_tasks_own_does_not_count(case: Case, failure_class: str) -> None:
    case.failures(failure_class=failure_class)

    assert "task_id" in case.submit(expect_success=True)


@pytest.mark.parametrize("failure_class", ["timeout", "provider_capacity"])
def test_transient_failures_never_open_the_breaker(case: Case, failure_class: str) -> None:
    case.failures(failure_class=failure_class, status="timeout", terminal_reason="timeout")

    assert "task_id" in case.submit(expect_success=True)


def test_the_streak_must_be_three_consecutive(case: Case) -> None:
    case.record("a", ago=timedelta(minutes=50))
    case.record("b", ago=timedelta(minutes=40))
    case.record("ok", status="completed", failure_class=None, ago=timedelta(minutes=30))
    case.record("c", ago=timedelta(minutes=20))
    case.record("d", ago=timedelta(minutes=10))

    assert "task_id" in case.submit(expect_success=True)


def test_the_streak_must_be_the_same_class(case: Case) -> None:
    case.record("a", ago=timedelta(minutes=50))
    case.record("b", ago=timedelta(minutes=40))
    case.record(
        "e",
        failure_class="launch_error",
        terminal_reason="launch_error",
        ago=timedelta(minutes=30),
    )

    assert "task_id" in case.submit(expect_success=True)


def test_only_the_most_recent_three_are_looked_at(case: Case) -> None:
    case.failures(ago=timedelta(minutes=60))
    case.record("recovered", status="completed", failure_class=None, ago=timedelta(minutes=10))

    assert "task_id" in case.submit(expect_success=True)


def test_another_workers_failures_are_not_held_against_this_one(case: Case) -> None:
    case.failures(worker="opencode")

    assert "task_id" in case.submit(expect_success=True)


def test_the_default_task_root_is_not_consulted_for_another_one(case: Case) -> None:
    # The history sits in the default ledger; this case submits to its own root.
    default = case.workspace.repo / ".claude" / "logs" / "delegate"
    for i in range(3):
        store.write_json(
            default / f"d-{i}" / "state.json",
            {
                "task_id": f"d-{i}",
                "role": "artifact-auditor",
                "worker": "codex",
                "status": "failed",
                "failure_class": "auth_failed",
                "terminal_reason": "nonzero_exit",
                "terminal_at": stamp(timedelta(minutes=30 + i)),
            },
        )

    assert "task_id" in case.submit(expect_success=True)


def test_allow_unhealthy_permits_a_deliberate_retry_and_records_it(case: Case) -> None:
    case.failures()
    handle = case.submit("--allow-unhealthy", expect_success=True)

    state = case.state(handle)
    assert state["unhealthy_override"] == {
        "failure_class": "auth_failed",
        "task_ids": ["old-2", "old-1", "old-0"],
    }
    assert state["health_probe"] is False


def test_an_explicit_worker_is_checked_too(case: Case) -> None:
    use_compare(case)
    case.failures(worker="opencode")

    assert case.refused("--worker", "opencode")["worker"] == "opencode"


def test_an_explicit_worker_is_not_swapped_even_in_a_comparing_role(case: Case) -> None:
    use_compare(case)
    case.failures(worker="opencode")

    out = case.refused("--worker", "opencode", role="ab")

    assert (out["error"], out["worker"]) == ("worker_unhealthy", "opencode")


def test_a_comparison_moves_to_the_healthy_arm_and_shows_it(case: Case) -> None:
    use_compare(case)
    case.failures(worker="codex")

    handle = case.submit(role="ab", expect_success=True)

    state = case.state(handle)
    assert state["worker"] == "opencode"
    assert (state["route_arm"], state["route_policy"]) == ("opencode", "compare")
    assert state["route_fallback_from"] == "codex"
    assert handle["route_fallback"]["from"] == "codex"
    assert handle["route_fallback"]["to"] == "opencode"
    assert "--allow-unhealthy" in handle["route_fallback"]["reason"]
    assert "cooldown" in handle["route_fallback"]["reason"]


def test_a_comparison_with_both_arms_broken_is_refused(case: Case) -> None:
    use_compare(case)
    case.failures(worker="codex")
    for index in range(3):
        case.record(f"oc-{index}", worker="opencode", ago=timedelta(minutes=30 + index))

    out = case.refused(role="ab")

    assert out["error"] == "worker_unhealthy"
    assert out["worker"] == "codex"
    assert out["other_arms"][0]["worker"] == "opencode"


def test_allow_unhealthy_keeps_the_arm_the_alternation_chose(case: Case) -> None:
    use_compare(case)
    case.failures(worker="codex")

    handle = case.submit("--allow-unhealthy", role="ab", expect_success=True)

    state = case.state(handle)
    assert (state["worker"], state["route_fallback_from"]) == ("codex", None)
    assert "route_fallback" not in handle


def test_the_path_helper_is_not_confused_by_a_missing_ledger(tmp_path: Path) -> None:
    assert tasks.recent_unhealthy(tmp_path / "nowhere", "codex") is None


# The circuit breaker: a worker that has been quiet for the cooldown gets one more try.


def test_the_refusal_names_when_the_cooldown_ends_and_the_override(case: Case) -> None:
    case.failures(ago=timedelta(hours=1))

    out = case.refused()

    until = NOW - timedelta(hours=1) + COOLDOWN
    assert out["cooldown_until"] == until.isoformat(timespec="seconds")
    assert out["cooldown_until"] in out["reason"]
    assert "--allow-unhealthy" in out["reason"]
    assert case.exit_code() == WORKER_UNHEALTHY


def test_after_the_cooldown_one_submit_goes_through_as_a_probe(case: Case) -> None:
    case.failures(ago=timedelta(hours=7))

    handle = case.submit(expect_success=True)

    state = case.state(handle)
    assert state["health_probe"] is True
    assert state["unhealthy_override"] is None


def test_the_cooldown_counts_from_the_last_failure_of_the_streak(case: Case) -> None:
    # Two old failures and a recent third: the streak's last failure is what counts.
    case.record("a", ago=timedelta(hours=20))
    case.record("b", ago=timedelta(hours=19))
    case.record("c", ago=timedelta(hours=1))

    assert case.refused()["error"] == "worker_unhealthy"


def test_a_probe_that_succeeds_clears_the_streak(case: Case) -> None:
    case.failures(ago=timedelta(hours=7))
    case.workspace.mode("success")
    probe = case.submit(title="probe", expect_success=True)
    assert case.wait(probe)["status"] == "completed"

    again = case.submit(title="after", expect_success=True)

    assert case.state(again)["health_probe"] is False


def test_a_probe_that_fails_again_reopens_the_breaker(case: Case) -> None:
    workspace = case.workspace
    workspace.settings.write_text(
        workspace.settings.read_text(encoding="utf-8").replace(
            'adapter = "codex"', 'adapter = "opencode"'
        ),
        encoding="utf-8",
    )
    case.failures(ago=timedelta(hours=7))
    workspace.mode("opencode_expired_api_key")
    probe = case.submit(title="probe", expect_success=True)
    assert case.wait(probe)["failure_class"] == "auth_failed"

    out = case.refused(title="next")

    assert out["error"] == "worker_unhealthy"
    assert out["task_ids"][0] == probe["task_id"]
    ends = datetime.fromisoformat(out["cooldown_until"])
    assert ends > datetime.now().astimezone() + timedelta(hours=5)


def test_the_cooldown_is_configurable(case: Case) -> None:
    case.workspace.settings.write_text(
        "worker_health_cooldown_hours = 1\n" + SETTINGS, encoding="utf-8"
    )
    case.failures(ago=timedelta(minutes=30))
    assert case.refused()["error"] == "worker_unhealthy"

    for stale in case.ledger.glob("old-*"):
        (stale / "state.json").unlink()
    case.failures(ago=timedelta(hours=2))
    assert case.state(case.submit(expect_success=True))["health_probe"] is True


@pytest.mark.parametrize("value", ["-1", '"6"', "true"])
def test_a_bad_cooldown_is_rejected_when_the_config_loads(value: str) -> None:
    text = f"worker_health_cooldown_hours = {value}\n" + SETTINGS
    with pytest.raises(config.ConfigError, match="worker_health_cooldown_hours"):
        config.parse(text.encode(), Path("delegate.toml"))


def test_the_default_cooldown_is_six_hours() -> None:
    assert config.parse(SETTINGS.encode(), Path("d.toml")).worker_health_cooldown_hours == 6


def test_health_of_is_decided_by_the_clock_it_is_given(tmp_path: Path) -> None:
    for i in range(3):
        store.write_json(
            tmp_path / f"t{i}" / "state.json",
            {
                "task_id": f"t{i}",
                "worker": "codex",
                "status": "failed",
                "failure_class": "auth_failed",
                "terminal_reason": "nonzero_exit",
                "terminal_at": f"2026-01-01T00:0{i}:00+00:00",
            },
        )
    health = tasks.health_of(tmp_path, "codex", 6)
    assert health is not None
    assert health.until == datetime.fromisoformat("2026-01-01T06:02:00+00:00")
    assert not health.probing(health.until - timedelta(seconds=1))
    assert health.probing(health.until)


def test_a_comparison_falls_back_from_a_cooling_arm_and_probes_a_cooled_one(
    case: Case,
) -> None:
    use_compare(case)
    case.failures(worker="codex", ago=timedelta(hours=1))
    for i in range(3):
        case.record(f"oc-{i}", worker="opencode", ago=timedelta(hours=8, minutes=i))

    handle = case.submit(role="ab", expect_success=True)

    state = case.state(handle)
    assert state["worker"] == "opencode"
    assert state["health_probe"] is True
    assert handle["route_fallback"]["from"] == "codex"


def test_an_identical_running_task_is_returned_rather_than_refused(case: Case) -> None:
    running = case.submit(expect_success=True)
    case.failures()

    again = case.submit(expect_success=True)

    assert again["task_id"] == running["task_id"]
    assert again["deduplicated"] is True
    # A different request is still held back.
    assert case.refused(title="something else")["error"] == "worker_unhealthy"
