"""Roles say what they need; the worker registry says what is available.

A role asks for a capability, never for a model name, so the same roles work
whichever backend is configured. Nothing is enabled by default: until a worker is
declared, delegation simply does not happen.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from delegate import config

SETTINGS = """
default_worker = "codex"

[workers.codex]
adapter = "codex"
enabled = true
models = { light = "luna", standard = "terra", frontier = "sol" }

[workers.opencode]
adapter = "opencode"
enabled = true
models = { standard = "anthropic/claude-sonnet-5" }

[roles.artifact-auditor]
capability = "standard"
effort = "high"
task_class = "review"

[roles.adversarial-critic]
capability = "frontier"
effort = "xhigh"
task_class = "review"
timeout = 3600
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "delegate.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_role_resolves_to_the_default_workers_model(tmp_path: Path) -> None:
    settings = config.load(write(tmp_path, SETTINGS))

    plan = settings.plan("artifact-auditor")

    assert (plan.worker, plan.adapter, plan.model, plan.effort) == (
        "codex",
        "codex",
        "terra",
        "high",
    )
    assert plan.timeout == config.DEFAULT_TIMEOUT


def test_a_role_may_carry_its_own_timeout(tmp_path: Path) -> None:
    settings = config.load(write(tmp_path, SETTINGS))

    assert settings.plan("adversarial-critic").timeout == 3600


def test_the_worker_and_the_effort_can_be_chosen_per_task(tmp_path: Path) -> None:
    settings = config.load(write(tmp_path, SETTINGS))

    plan = settings.plan("artifact-auditor", worker="opencode", effort="max")

    assert (plan.worker, plan.model, plan.effort) == (
        "opencode",
        "anthropic/claude-sonnet-5",
        "max",
    )


def test_an_unknown_role_is_named_in_the_error(tmp_path: Path) -> None:
    settings = config.load(write(tmp_path, SETTINGS))

    with pytest.raises(config.ConfigError, match="unknown role: reviewer"):
        settings.plan("reviewer")


def test_with_no_worker_enabled_nothing_is_delegated(tmp_path: Path) -> None:
    settings = config.load(
        write(
            tmp_path,
            """
[workers.codex]
adapter = "codex"
enabled = false
models = { standard = "terra" }

[roles.artifact-auditor]
capability = "standard"
effort = "high"
task_class = "review"
""",
        )
    )

    with pytest.raises(config.ConfigError, match="no worker is enabled"):
        settings.plan("artifact-auditor")


def test_a_worker_without_a_model_for_the_capability_is_refused_before_launch(
    tmp_path: Path,
) -> None:
    settings = config.load(write(tmp_path, SETTINGS))

    with pytest.raises(config.ConfigError, match="opencode has no model for capability frontier"):
        settings.plan("adversarial-critic", worker="opencode")


def test_choosing_a_worker_that_is_not_declared_is_refused(tmp_path: Path) -> None:
    settings = config.load(write(tmp_path, SETTINGS))

    with pytest.raises(config.ConfigError, match="unknown worker: cursor"):
        settings.plan("artifact-auditor", worker="cursor")


def test_choosing_a_declared_but_disabled_worker_is_refused(tmp_path: Path) -> None:
    settings = config.load(
        write(
            tmp_path,
            SETTINGS.replace(
                'adapter = "opencode"\nenabled = true', 'adapter = "opencode"\nenabled = false'
            ),
        )
    )

    with pytest.raises(config.ConfigError, match="opencode is declared but not enabled"):
        settings.plan("artifact-auditor", worker="opencode")


def test_a_default_worker_that_is_not_enabled_is_an_error_not_a_silent_substitution(
    tmp_path: Path,
) -> None:
    settings = config.load(
        write(tmp_path, SETTINGS.replace('default_worker = "codex"', 'default_worker = "cursor"'))
    )

    with pytest.raises(config.ConfigError, match="default_worker cursor is not an enabled worker"):
        settings.plan("artifact-auditor")


def test_several_enabled_workers_with_no_default_must_be_disambiguated(tmp_path: Path) -> None:
    settings = config.load(write(tmp_path, SETTINGS.replace('default_worker = "codex"\n', "")))

    with pytest.raises(config.ConfigError, match="set default_worker or pass --worker"):
        settings.plan("artifact-auditor")
