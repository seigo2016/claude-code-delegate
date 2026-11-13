"""One role split across two workers, so their answers can be compared."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import SETTINGS, Workspace
from delegate import config

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
