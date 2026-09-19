"""Roles say what level of worker they need; a worker says what that level means.

Nothing is enabled by default: until a worker is declared, delegation does not
happen.
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
light = { model = "luna", effort = "high" }
standard = { model = "terra", effort = "high" }
frontier = { model = "sol", effort = "xhigh" }

[workers.opencode]
adapter = "opencode"
enabled = true
standard = { model = "kimi", effort = "high" }

[roles.artifact-auditor]
level = "standard"
forbids_allowed_writes = true
repo_local_reads = true
allowed_read_roots = ["/mnt/research-data"]

[roles.adversarial-critic]
level = "frontier"
timeout = 3600
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "delegate.toml"
    path.write_text(text, encoding="utf-8")
    return path


def load(tmp_path: Path, text: str = SETTINGS) -> config.Settings:
    return config.load(write(tmp_path, text))


def test_a_role_resolves_through_the_level_the_default_worker_defines(tmp_path: Path) -> None:
    plan = load(tmp_path).plan("artifact-auditor")

    assert (plan.worker, plan.adapter, plan.model, plan.effort) == (
        "codex",
        "codex",
        "terra",
        "high",
    )
    assert plan.timeout == config.DEFAULT_TIMEOUT
    assert plan.role.forbids_allowed_writes is True
    assert plan.role.repo_local_reads is True
    assert plan.role.allowed_read_roots == ("/mnt/research-data",)


def test_the_same_role_resolves_differently_on_another_worker(tmp_path: Path) -> None:
    plan = load(tmp_path).plan("artifact-auditor", worker="opencode")

    assert (plan.worker, plan.model, plan.effort) == ("opencode", "kimi", "high")


def test_a_role_may_carry_its_own_timeout(tmp_path: Path) -> None:
    assert load(tmp_path).plan("adversarial-critic").timeout == 3600


def test_a_role_may_carry_instructions(tmp_path: Path) -> None:
    settings_text = """
    default_worker = "codex"
    [workers.codex]
    adapter = "codex"
    enabled = true
    light = { model = "luna", effort = "high" }
    [roles.patcher]
    level = "light"
    instructions = "Check all inputs before run."
    """
    assert (
        load(tmp_path, settings_text).plan("patcher").role.instructions
        == "Check all inputs before run."
    )


def test_an_unknown_role_is_named_in_the_error(tmp_path: Path) -> None:
    with pytest.raises(config.ConfigError, match="unknown role: reviewer"):
        load(tmp_path).plan("reviewer")


def test_with_no_worker_enabled_nothing_is_delegated(tmp_path: Path) -> None:
    with pytest.raises(config.ConfigError, match="no worker is enabled"):
        load(tmp_path, SETTINGS.replace("enabled = true", "enabled = false")).plan(
            "artifact-auditor"
        )


def test_a_worker_that_does_not_define_the_level_is_refused_before_launch(
    tmp_path: Path,
) -> None:
    with pytest.raises(config.ConfigError, match="opencode does not define level frontier"):
        load(tmp_path).plan("adversarial-critic", worker="opencode")


def test_choosing_a_worker_that_is_not_declared_is_refused(tmp_path: Path) -> None:
    with pytest.raises(config.ConfigError, match="unknown worker: cursor"):
        load(tmp_path).plan("artifact-auditor", worker="cursor")


def test_choosing_a_declared_but_disabled_worker_is_refused(tmp_path: Path) -> None:
    text = SETTINGS.replace(
        'adapter = "opencode"\nenabled = true', 'adapter = "opencode"\nenabled = false'
    )

    with pytest.raises(config.ConfigError, match="opencode is declared but not enabled"):
        load(tmp_path, text).plan("artifact-auditor", worker="opencode")


def test_a_default_worker_that_is_not_enabled_is_an_error_not_a_silent_substitution(
    tmp_path: Path,
) -> None:
    text = SETTINGS.replace('default_worker = "codex"', 'default_worker = "cursor"')

    with pytest.raises(config.ConfigError, match="default_worker cursor is not an enabled worker"):
        load(tmp_path, text).plan("artifact-auditor")


def test_several_enabled_workers_with_no_default_must_be_disambiguated(tmp_path: Path) -> None:
    text = SETTINGS.replace('default_worker = "codex"\n', "")

    with pytest.raises(config.ConfigError, match="set default_worker or pass --worker"):
        load(tmp_path, text).plan("artifact-auditor")


def test_a_role_without_a_level_is_reported_not_raised_as_a_key_error(tmp_path: Path) -> None:
    with pytest.raises(config.ConfigError, match="roles.artifact-auditor is missing level"):
        load(tmp_path, "[roles.artifact-auditor]\ntimeout = 60\n")


def test_a_level_without_a_model_or_an_effort_is_reported(tmp_path: Path) -> None:
    text = SETTINGS.replace(
        'standard = { model = "terra", effort = "high" }', 'standard = { model = "terra" }'
    )

    with pytest.raises(config.ConfigError, match="workers.codex.standard is missing effort"):
        load(tmp_path, text)
