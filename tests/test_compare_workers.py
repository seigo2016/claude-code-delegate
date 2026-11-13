"""One role split across two workers, so their answers can be compared."""

from __future__ import annotations

import contextlib
import json
import threading
from pathlib import Path

import pytest

from conftest import PACKET, SETTINGS, Workspace
from delegate import config, store, tasks

COMPARE = """
[workers.opencode]
adapter = "opencode"
enabled = true
standard = { model = "sonnet", effort = "high" }

[roles.ab]
level = "standard"
compare_workers = ["codex", "opencode"]
"""


@pytest.fixture
def compare(workspace: Workspace) -> Workspace:
    workspace.settings.write_text(SETTINGS + COMPARE, encoding="utf-8")
    workspace.mode("utterly_silent")
    return workspace


def state_of(workspace: Workspace, handle: dict) -> dict:
    return workspace.run("status", handle["task_id"])


def test_submits_alternate_and_record_the_arm(compare: Workspace) -> None:
    handles = [compare.submit(role="ab", title=f"t{i}", expect_success=True) for i in range(4)]
    states = [state_of(compare, handle) for handle in handles]

    assert [state["worker"] for state in states] == ["codex", "opencode", "codex", "opencode"]
    assert [state["route_arm"] for state in states] == [state["worker"] for state in states]
    assert {state["route_policy"] for state in states} == {"compare"}


def test_without_compare_workers_the_preferred_worker_stands(compare: Workspace) -> None:
    compare.settings.write_text(
        SETTINGS
        + COMPARE.replace(
            'compare_workers = ["codex", "opencode"]', 'preferred_worker = "opencode"'
        ),
        encoding="utf-8",
    )
    state = state_of(compare, compare.submit(role="ab", expect_success=True))

    assert state["worker"] == "opencode"
    assert (state["route_arm"], state["route_policy"]) == (None, None)


def test_an_explicit_worker_wins_and_does_not_advance_the_alternation(compare: Workspace) -> None:
    forced = state_of(
        compare, compare.submit("--worker", "opencode", role="ab", title="a", expect_success=True)
    )
    first = state_of(compare, compare.submit(role="ab", title="b", expect_success=True))

    assert forced["worker"] == "opencode"
    assert (forced["route_policy"], forced["route_arm"]) == ("explicit", None)
    assert (first["worker"], first["route_policy"]) == ("codex", "compare")


def test_identical_requests_are_not_merged_across_arms(compare: Workspace) -> None:
    first = compare.submit(role="ab", expect_success=True)
    second = compare.submit(role="ab", expect_success=True)
    third = compare.submit(role="ab", expect_success=True)

    assert first["worker"] == "codex" and second["worker"] == "opencode"
    assert second["task_id"] != first["task_id"] and not second["deduplicated"]
    # The alternation is back on the first arm, where the identical task is still running.
    assert third["task_id"] == first["task_id"] and third["deduplicated"]


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ('["codex"]', "at least two"),
        ('["codex", "codex"]', "distinct"),
        ('["codex", "nobody"]', "nobody"),
        ('"codex"', "array"),
        ("[1, 2]", "array"),
    ],
)
def test_compare_workers_are_validated_when_the_config_loads(value: str, message: str) -> None:
    body = COMPARE.replace('["codex", "opencode"]', value)
    with pytest.raises(config.ConfigError, match=message):
        config.parse((SETTINGS + body).encode(), Path("delegate.toml"))


def test_a_bad_compare_workers_is_reported_by_submit(compare: Workspace) -> None:
    compare.settings.write_text(
        SETTINGS + COMPARE.replace('["codex", "opencode"]', '["codex"]'), encoding="utf-8"
    )
    out = compare.submit(role="ab")

    assert "compare_workers" in out["error"]


def test_a_disabled_arm_is_rejected_when_the_config_loads() -> None:
    body = COMPARE.replace("enabled = true", "enabled = false")
    with pytest.raises(config.ConfigError, match="not enabled: opencode"):
        config.parse((SETTINGS + body).encode(), Path("delegate.toml"))


def drop_opencode(workspace: Workspace) -> None:
    """Leave only the codex stand-in on PATH, as if opencode were not installed."""
    (workspace.worker_path.parent / "opencode").unlink()
    workspace.env["PATH"] = str(workspace.worker_path.parent)


def test_an_arm_whose_binary_is_missing_falls_back_to_the_other_and_says_so(
    compare: Workspace,
) -> None:
    drop_opencode(compare)
    first = compare.submit(role="ab", title="t0", expect_success=True)
    second = compare.submit(role="ab", title="t1", expect_success=True)

    assert "route_fallback" not in first
    assert second["worker"] == "codex"
    assert second["route_fallback"]["from"] == "opencode"
    assert second["route_fallback"]["to"] == "codex"
    assert "opencode" in second["route_fallback"]["reason"]
    state = state_of(compare, second)
    assert (state["route_arm"], state["route_fallback_from"]) == ("codex", "opencode")
    assert "opencode" in state["route_fallback_reason"]


def test_an_arm_that_cannot_be_planned_falls_back_to_the_other(compare: Workspace) -> None:
    compare.settings.write_text(
        (SETTINGS + COMPARE).replace(
            'standard = { model = "sonnet", effort = "high" }',
            'light = { model = "sonnet", effort = "high" }',
        ),
        encoding="utf-8",
    )
    compare.submit(role="ab", title="t0", expect_success=True)
    second = compare.submit(role="ab", title="t1", expect_success=True)

    assert second["worker"] == "codex"
    assert second["route_fallback"]["from"] == "opencode"
    assert "standard" in second["route_fallback"]["reason"]


def test_a_role_with_no_runnable_arm_is_refused(compare: Workspace) -> None:
    compare.remove_worker()
    (compare.worker_path.parent / "opencode").unlink()

    out = compare.submit(role="ab")

    assert "task_id" not in out
    assert "worker binary not on PATH: codex" in out["error"]
    assert out["other_arms"][0]["worker"] == "opencode"


def history(ledger: Path, task_id: str, **fields: object) -> None:
    """A task an earlier submit left in the ledger, with only the fields routing reads."""
    store.write_json(
        ledger / task_id / "state.json",
        {
            "task_id": task_id,
            "role": "ab",
            "status": "completed",
            "route_policy": "compare",
            "route_arm": "codex",
            "worker": "codex",
            **fields,
        },
    )


def next_worker(compare: Workspace, ledger: Path, title: str = "next") -> str:
    handle = compare.submit("--task-root", str(ledger), role="ab", title=title, expect_success=True)
    return handle["worker"]


def test_only_this_roles_comparing_tasks_move_the_alternation(
    compare: Workspace, tmp_path: Path
) -> None:
    ledger = tmp_path / "ledger"
    history(ledger, "other-role", role="cd", route_arm="codex")

    assert next_worker(compare, ledger) == "codex"


def test_an_explicit_choice_does_not_move_the_alternation_even_with_an_arm_recorded(
    compare: Workspace, tmp_path: Path
) -> None:
    ledger = tmp_path / "ledger"
    history(ledger, "explicit", route_policy="explicit", route_arm="codex")

    assert next_worker(compare, ledger) == "codex"


def test_a_task_cancelled_before_it_started_does_not_count(
    compare: Workspace, tmp_path: Path
) -> None:
    ledger = tmp_path / "ledger"
    history(
        ledger,
        "never-ran",
        status="cancelled",
        terminal_reason="cancelled_before_start",
        route_arm="codex",
    )

    assert next_worker(compare, ledger) == "codex"


def test_a_task_cancelled_after_it_started_still_counts(compare: Workspace, tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    history(ledger, "ran", status="cancelled", started_at="2026-11-01T00:00:00+00:00")

    assert next_worker(compare, ledger) == "opencode"


def test_a_fallback_counts_for_the_arm_that_was_asked_for(
    compare: Workspace, tmp_path: Path
) -> None:
    ledger = tmp_path / "ledger"
    # codex was due, could not run, and opencode did the work in its place.
    history(
        ledger,
        "fell-back",
        worker="opencode",
        route_arm="opencode",
        route_fallback_from="codex",
    )

    assert next_worker(compare, ledger) == "opencode"


def test_two_submits_at_once_take_different_arms(
    compare: Workspace, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("PATH", compare.env["PATH"])
    monkeypatch.setattr(tasks.subprocess, "Popen", lambda *a, **k: type("P", (), {"pid": 1})())
    settings = config.parse(compare.settings.read_bytes(), compare.settings)
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    # Both submits get past the choice of arm together, unless something makes one wait.
    meeting = threading.Barrier(2, timeout=1.5)
    choose = tasks._route

    def choose_then_meet(*args, **kwargs):
        chosen = choose(*args, **kwargs)
        with contextlib.suppress(threading.BrokenBarrierError):
            meeting.wait()
        return chosen

    monkeypatch.setattr(tasks, "_route", choose_then_meet)
    workers: list[str] = []

    def submit(title: str) -> None:
        handle = tasks.submit(
            project_root=compare.repo,
            settings=settings,
            role="ab",
            title=title,
            value=json.loads(PACKET),
            ledger=ledger,
        )
        workers.append(handle["worker"])

    threads = [threading.Thread(target=submit, args=(f"t{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(workers) == ["codex", "opencode"]
