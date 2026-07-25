"""Codex specifics that the shared adapter contract does not reach."""

from __future__ import annotations

import json
from pathlib import Path

from delegate.adapters import codex

ADAPTER = codex.CodexAdapter()


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
    )

    assert command[:4] == ["codex", "exec", "-C", str(tmp_path)]
    assert str(tmp_path / "result.schema.json") in command
    assert command[-3:] == ["--disable", "fast_mode", "-"]


def test_a_finished_agent_message_carries_its_text() -> None:
    raw = json.dumps(
        {"type": "item.completed", "item": {"id": "i", "type": "agent_message", "text": "{}"}}
    )

    (event,) = ADAPTER.parse_events(raw)

    assert (event.kind, event.item_type, event.text) == ("item_completed", "agent_message", "{}")
