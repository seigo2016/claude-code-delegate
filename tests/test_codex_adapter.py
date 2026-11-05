"""Codex specifics that the shared adapter contract does not reach."""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TypedDict

import pytest

from delegate.adapters import codex

ADAPTER = codex.CodexAdapter()


def test_cache_root_on_darwin_respects_xdg_cache_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(codex.sys, "platform", "darwin")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert codex.cache_root() == tmp_path

    monkeypatch.delenv("XDG_CACHE_HOME")
    assert codex.cache_root() == Path.home() / "Library" / "Caches"


class CommandOptions(TypedDict):
    project_root: Path
    prompt_path: Path
    result_path: Path
    schema_path: Path
    model: str
    effort: str
    writes_allowed: bool
    runs_commands: bool


def isolated(base: Path, source: Path) -> Path:
    key = hashlib.sha256(os.fsencode(str(source.resolve()))).hexdigest()[:24]
    return base / key


def test_a_repeated_model_refresh_failure_is_a_runtime_warning() -> None:
    line = (
        "ERROR codex_models_manager::manager: failed to refresh available models: "
        "timeout waiting for child process to exit"
    )

    (event,) = ADAPTER.parse_stderr_lines(line)

    assert event.kind == "runtime_warning"


def test_the_command_pins_the_root_the_result_contract_and_full_reasoning(tmp_path: Path) -> None:
    command = ADAPTER.build_command(
        project_root=tmp_path,
        prompt_path=tmp_path / "prompt.md",
        result_path=tmp_path / "result.json",
        schema_path=tmp_path / "result.schema.json",
        model="gpt-5.6-terra",
        effort="high",
        writes_allowed=True,
        runs_commands=False,
    )

    codex_idx = command.index("codex")
    assert command[codex_idx : codex_idx + 4] == ["codex", "exec", "-C", str(tmp_path)]
    assert str(tmp_path / "result.schema.json") in command
    assert command[-3:] == ["--disable", "fast_mode", "-"]


def test_resume_preserves_isolated_home_and_original_options(tmp_path: Path) -> None:
    adapter = codex.CodexAdapter(
        isolate_home=True, target_home=tmp_path / "isolated", user_home=tmp_path / "user"
    )
    options: CommandOptions = dict(
        project_root=tmp_path,
        prompt_path=tmp_path / "prompt",
        result_path=tmp_path / "result",
        schema_path=tmp_path / "schema",
        model="model",
        effort="high",
        writes_allowed=True,
        runs_commands=False,
    )
    initial = adapter.build_command(**options)
    resumed = adapter.build_resume_command(session_id="session-1", **options)
    assert resumed == [*initial[:-1], "resume", "session-1", "-"]
    expected = isolated(tmp_path / "isolated", tmp_path / "user")
    assert resumed[:2] == ["env", f"CODEX_HOME={expected}"]


def test_external_write_roots_are_added_to_the_codex_sandbox(tmp_path: Path) -> None:
    external_root = tmp_path / "external-output"
    command = ADAPTER.build_command(
        project_root=tmp_path,
        prompt_path=tmp_path / "prompt.md",
        result_path=tmp_path / "result.json",
        schema_path=tmp_path / "result.schema.json",
        model="gpt-5.6-terra",
        effort="high",
        writes_allowed=False,
        runs_commands=True,
        allowed_write_roots=(str(external_root),),
    )

    setting = next(
        value for value in command if value.startswith("sandbox_workspace_write.writable_roots=")
    )
    roots = json.loads(setting.split("=", 1)[1])
    assert str(external_root) in roots


def test_codex_home_points_to_dedicated_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_dir = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_dir))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "user_codex"))

    command = ADAPTER.build_command(
        project_root=tmp_path,
        prompt_path=tmp_path / "prompt.md",
        result_path=tmp_path / "result.json",
        schema_path=tmp_path / "result.schema.json",
        model="gpt-5.6-terra",
        effort="high",
        writes_allowed=True,
        runs_commands=False,
    )

    expected_home = isolated(cache_dir / "delegate" / "codex-home", tmp_path / "user_codex")
    assert command[:2] == ["env", f"CODEX_HOME={expected_home}"]
    assert expected_home.is_dir()


def test_auth_json_is_symlink_without_reading_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_home = tmp_path / "user_codex"
    user_home.mkdir()
    auth_file = user_home / "auth.json"
    auth_file.write_text('{"token": "secret"}', encoding="utf-8")
    # Make auth_file unreadable so reading contents would raise PermissionError
    auth_file.chmod(0o000)

    cache_dir = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_dir))
    monkeypatch.setenv("CODEX_HOME", str(user_home))

    try:
        ADAPTER.build_command(
            project_root=tmp_path,
            prompt_path=tmp_path / "prompt.md",
            result_path=tmp_path / "result.json",
            schema_path=tmp_path / "result.schema.json",
            model="gpt-5.6-terra",
            effort="high",
            writes_allowed=True,
            runs_commands=False,
        )
        isolated_home = isolated(cache_dir / "delegate" / "codex-home", user_home)
        link_path = isolated_home / "auth.json"
        assert link_path.is_symlink()
        assert Path(os.readlink(link_path)) == auth_file.resolve()
    finally:
        auth_file.chmod(0o600)


def test_agents_md_is_absent_and_minimal_config_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_home = tmp_path / "user_codex"
    user_home.mkdir()
    (user_home / "AGENTS.md").write_text("bootstrap superpowers", encoding="utf-8")
    (user_home / "config.toml").write_text('personality = "chatty"\n', encoding="utf-8")

    cache_dir = tmp_path / "cache"
    isolated_home = isolated(cache_dir / "delegate" / "codex-home", user_home)
    isolated_home.mkdir(parents=True)
    # Pre-populate dirty AGENTS.md in isolated home to ensure it gets removed
    dirty_agents = isolated_home / "AGENTS.md"
    dirty_agents.write_text("old instructions", encoding="utf-8")

    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_dir))
    monkeypatch.setenv("CODEX_HOME", str(user_home))

    ADAPTER.build_command(
        project_root=tmp_path,
        prompt_path=tmp_path / "prompt.md",
        result_path=tmp_path / "result.json",
        schema_path=tmp_path / "result.schema.json",
        model="gpt-5.6-terra",
        effort="high",
        writes_allowed=True,
        runs_commands=False,
    )

    assert not (isolated_home / "AGENTS.md").exists()
    config_text = (isolated_home / "config.toml").read_text(encoding="utf-8")
    assert 'approval_policy = "never"' in config_text
    assert 'sandbox_mode = "read-only"' in config_text


def test_broken_symlink_is_recreated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    user_home = tmp_path / "user_codex"
    user_home.mkdir()
    auth_file = user_home / "auth.json"
    auth_file.write_text('{"token": "valid"}', encoding="utf-8")

    cache_dir = tmp_path / "cache"
    isolated_home = isolated(cache_dir / "delegate" / "codex-home", user_home)
    isolated_home.mkdir(parents=True)
    broken_link = isolated_home / "auth.json"
    broken_link.symlink_to(tmp_path / "does_not_exist" / "auth.json")
    assert broken_link.is_symlink()
    assert not broken_link.exists()

    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_dir))
    monkeypatch.setenv("CODEX_HOME", str(user_home))

    ADAPTER.build_command(
        project_root=tmp_path,
        prompt_path=tmp_path / "prompt.md",
        result_path=tmp_path / "result.json",
        schema_path=tmp_path / "result.schema.json",
        model="gpt-5.6-terra",
        effort="high",
        writes_allowed=True,
        runs_commands=False,
    )

    assert broken_link.is_symlink()
    assert broken_link.exists()
    assert Path(os.readlink(broken_link)) == auth_file.resolve()


def test_isolate_home_false_preserves_original_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. Via build_command parameter
    command = ADAPTER.build_command(
        project_root=tmp_path,
        prompt_path=tmp_path / "prompt.md",
        result_path=tmp_path / "result.json",
        schema_path=tmp_path / "result.schema.json",
        model="gpt-5.6-terra",
        effort="high",
        writes_allowed=True,
        runs_commands=False,
        isolate_home=False,
    )
    assert command[:4] == ["codex", "exec", "-C", str(tmp_path)]

    # 2. Via delegate.toml configuration
    config_dir = tmp_path / ".claude"
    config_dir.mkdir(parents=True)
    (config_dir / "delegate.toml").write_text(
        """
default_worker = "codex"
[workers.codex]
adapter = "codex"
enabled = true
isolate_home = false
standard = { model = "terra", effort = "high" }
""",
        encoding="utf-8",
    )

    command_from_config = ADAPTER.build_command(
        project_root=tmp_path,
        prompt_path=tmp_path / "prompt.md",
        result_path=tmp_path / "result.json",
        schema_path=tmp_path / "result.schema.json",
        model="gpt-5.6-terra",
        effort="high",
        writes_allowed=True,
        runs_commands=False,
    )
    assert command_from_config[:4] == ["codex", "exec", "-C", str(tmp_path)]


@pytest.mark.parametrize("relation", ["same", "source_parent", "target_parent", "symlink"])
def test_overlapping_homes_fall_back_without_modifying_source(
    tmp_path: Path, relation: str
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.toml").write_text("original")
    (source / "AGENTS.md").write_text("instructions")
    if relation == "same":
        base = source
    elif relation == "source_parent":
        base = source / "child"
    elif relation == "target_parent":
        base = tmp_path
    else:
        base = tmp_path / "alias"
        base.symlink_to(source, target_is_directory=True)
    adapter = codex.CodexAdapter(target_home=base, user_home=source)
    with pytest.warns(UserWarning, match="overlap"):
        command = adapter.build_command(
            project_root=tmp_path,
            prompt_path=tmp_path / "prompt",
            result_path=tmp_path / "result",
            schema_path=tmp_path / "schema",
            model="m",
            effort="high",
            writes_allowed=False,
            runs_commands=False,
        )
    assert command[0] == "codex"
    assert (source / "config.toml").read_text() == "original"
    assert (source / "AGENTS.md").read_text() == "instructions"


def test_concurrent_preparation_separates_sources_and_is_atomic(tmp_path: Path) -> None:
    base = tmp_path / "isolated"
    sources = [tmp_path / "user1", tmp_path / "user2"]
    for source in sources:
        source.mkdir()
        (source / "auth.json").write_text(source.name)
    with ThreadPoolExecutor(max_workers=16) as pool:
        homes = list(
            pool.map(
                lambda source: codex.prepare_codex_home(target_home=base, user_home=source),
                sources * 40,
            )
        )
    assert homes[0] != homes[1]
    for source, home in zip(sources * 40, homes, strict=True):
        assert home is not None
        assert (home / "auth.json").resolve() == source / "auth.json"
        assert (home / "config.toml").read_text() == codex.MINIMAL_CONFIG


def test_fast_command_changes_only_fast_options(tmp_path: Path) -> None:
    options: dict[str, Any] = dict(
        project_root=tmp_path,
        prompt_path=tmp_path / "prompt",
        result_path=tmp_path / "result",
        schema_path=tmp_path / "schema",
        model="model",
        effort="high",
        writes_allowed=False,
        runs_commands=False,
        isolate_home=False,
    )
    default = ADAPTER.build_command(**options)
    assert ADAPTER.build_command(**options, fast=False) == default
    fast = ADAPTER.build_command(**options, fast=True)
    assert fast == [
        *default[:-3],
        "-c",
        "features.fast_mode=true",
        "-c",
        'service_tier="fast"',
        "-",
    ]
    assert ADAPTER.build_resume_command(session_id="abc", **options, fast=True) == [
        *fast[:-1],
        "resume",
        "abc",
        "-",
    ]
