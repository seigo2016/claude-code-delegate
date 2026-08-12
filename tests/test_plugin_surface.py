"""The files Claude Code loads, checked for the mistakes a manifest cannot catch.

A plugin that declares a hook pointing at a missing script fails at runtime, in
someone else's session. These checks are cheap and catch that here instead.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from delegate import config

ROOT = Path(__file__).resolve().parents[1]


def read_frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} has no frontmatter"
    body = text.split("---\n", 2)[1]
    fields = {}
    for line in body.splitlines():
        if ": " in line:
            key, _, value = line.partition(": ")
            fields[key.strip()] = value.strip()
    return fields


def test_the_manifest_names_the_plugin_delegate() -> None:
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))

    assert manifest["name"] == "delegate"
    assert manifest["description"]
    assert manifest["version"]


def test_installing_the_plugin_changes_nothing_about_the_session_by_default() -> None:
    assert not (ROOT / "settings.json").exists(), (
        "a plugin-level settings.json would switch the main agent for everyone who installs it"
    )


def test_every_hook_points_at_a_command_that_exists() -> None:
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    entrypoint = ROOT / "bin" / "delegate"

    assert entrypoint.exists() and os.access(entrypoint, os.X_OK)
    for entries in hooks.values():
        for entry in entries:
            for hook in entry["hooks"]:
                assert "${CLAUDE_PLUGIN_ROOT}/bin/delegate" in hook["command"]


def test_the_finished_task_hook_wakes_the_session_instead_of_blocking_it() -> None:
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]

    (post,) = hooks["PostToolUse"]
    (hook,) = post["hooks"]

    assert post["matcher"] == "Bash"
    assert hook["asyncRewake"] is True
    assert hook["rewakeMessage"]
    assert "_hook-watch" in hook["command"]
    assert "CLAUDE_PROJECT_DIR" not in hook["command"]
    # asyncRewake is documented as implying async, but only backgrounds the hook in
    # an interactive session. Under `claude -p` this one would run in the foreground
    # and hold up the call that triggered it for the whole of its timeout.
    assert hook["async"] is True


def test_the_hook_waits_longer_than_a_task_is_allowed_to_run() -> None:
    # A watcher that gives up first leaves the finished task uncollected until
    # some later Bash call happens to start another one.
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]

    (hook,) = hooks["PostToolUse"][0]["hooks"]

    assert hook["timeout"] > config.DEFAULT_TIMEOUT


def test_the_skill_and_the_chair_agent_declare_themselves() -> None:
    skill = read_frontmatter(ROOT / "skills" / "delegate" / "SKILL.md")
    agent = read_frontmatter(ROOT / "agents" / "delegate-chair.md")

    assert skill["name"] == "delegate"
    assert skill["description"]
    assert agent["name"] == "delegate-chair"
    assert agent["description"]


def test_the_entrypoint_runs_without_the_package_being_installed(tmp_path: Path) -> None:
    result = subprocess.run(
        [str(ROOT / "bin" / "delegate"), "reconcile", "--project-root", str(tmp_path)],
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"tasks": [], "ready": [], "next_action": None}


def test_the_example_settings_enable_nothing_the_reader_did_not_choose() -> None:
    example = (ROOT / "examples" / "delegate.toml").read_text(encoding="utf-8")
    settings = [line for line in example.splitlines() if line.strip() and not line.startswith("#")]

    assert not [line for line in settings if "enabled = true" in line], (
        "an example must not switch a backend on by itself"
    )
    assert any(line.startswith("[roles.") for line in settings)
    assert any(line.startswith("[workers.") for line in settings)


def test_nothing_but_a_ready_task_exits_with_the_code_that_wakes_the_session(
    tmp_path: Path,
) -> None:
    # The launcher runs as an asyncRewake hook after every Bash call, where 2 is
    # the signal that a task is ready. Any other route to 2 wakes the session with
    # its own output standing in for the list of finished tasks.
    bad_flag = subprocess.run(
        [str(ROOT / "bin" / "delegate"), "watch", "--no-such-flag"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )

    assert bad_flag.returncode != 2, bad_flag.stderr


def test_the_entrypoint_says_what_is_missing_when_python_is_too_old(tmp_path: Path) -> None:
    # macOS ships 3.9 as python3, and tomllib arrived in 3.11. Failing with an
    # ImportError from a file the reader has never opened helps nobody.
    fake = tmp_path / "bin"
    fake.mkdir()
    for name in ("python3.14", "python3.13", "python3.12", "python3.11", "python3", "python"):
        stub = fake / name
        stub.write_text("#!/bin/sh\nexit 1\n")
        stub.chmod(0o755)

    result = subprocess.run(
        [str(ROOT / "bin" / "delegate"), "reconcile"],
        capture_output=True,
        text=True,
        env={"PATH": f"{fake}:/usr/bin:/bin"},
    )

    assert result.returncode not in (0, 2), "2 would wake the session with this message"
    assert "3.11" in result.stderr
