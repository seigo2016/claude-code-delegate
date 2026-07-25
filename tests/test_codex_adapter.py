"""The Codex adapter is the only place that may know Codex's vocabulary.

Everything downstream reads normalized events, so a Codex item type or a Codex
stderr string appearing anywhere else is a leak.
"""

from __future__ import annotations

import json
from pathlib import Path

from delegate.adapters import codex

ADAPTER = codex.CodexAdapter()


def test_the_thread_announcement_becomes_the_session_identity() -> None:
    event = ADAPTER.parse_event(json.dumps({"type": "thread.started", "thread_id": "th-1"}))

    assert event is not None
    assert event.kind == "session_started"
    assert event.session_id == "th-1"


def test_a_started_tool_call_keeps_its_identity_and_type() -> None:
    raw = json.dumps(
        {"type": "item.started", "item": {"id": "it-1", "type": "command_execution"}}
    )

    event = ADAPTER.parse_event(raw)

    assert event is not None
    assert (event.kind, event.item_id, event.item_type) == (
        "item_started",
        "it-1",
        "command_execution",
    )


def test_a_finished_agent_message_carries_its_text() -> None:
    raw = json.dumps(
        {"type": "item.completed", "item": {"id": "it-2", "type": "agent_message", "text": "{}"}}
    )

    event = ADAPTER.parse_event(raw)

    assert event is not None
    assert (event.kind, event.item_type, event.text) == ("item_completed", "agent_message", "{}")


def test_the_end_of_the_turn_is_reported() -> None:
    event = ADAPTER.parse_event(json.dumps({"type": "turn.completed"}))

    assert event is not None and event.kind == "turn_completed"


def test_lines_that_are_not_events_are_ignored() -> None:
    assert ADAPTER.parse_event("not json at all") is None
    assert ADAPTER.parse_event(json.dumps({"type": "something.unknown"})) is None
    assert ADAPTER.parse_event(json.dumps(["not", "an", "object"])) is None


def test_a_repeated_model_refresh_failure_is_a_runtime_warning() -> None:
    line = (
        "ERROR codex_models_manager::manager: failed to refresh available models: "
        "timeout waiting for child process to exit"
    )

    event = ADAPTER.parse_stderr_line(line)

    assert event is not None and event.kind == "runtime_warning"
    assert ADAPTER.parse_stderr_line("some unrelated warning") is None


def test_the_command_pins_the_root_model_effort_and_result_contract(tmp_path: Path) -> None:
    command = ADAPTER.build_command(
        project_root=tmp_path,
        result_path=tmp_path / "result.json",
        schema_path=tmp_path / "result.schema.json",
        model="gpt-5.6-terra",
        effort="high",
        resume_session_id=None,
    )

    assert command[:2] == ["codex", "exec"]
    assert ["-C", str(tmp_path)] == command[2:4]
    assert "--model" in command and command[command.index("--model") + 1] == "gpt-5.6-terra"
    assert 'model_reasoning_effort="high"' in command
    assert str(tmp_path / "result.schema.json") in command
    assert command[-1] == "-", "the prompt is fed on stdin"
    assert ["--disable", "fast_mode"] == command[-3:-1]


def test_resuming_reuses_the_recorded_session(tmp_path: Path) -> None:
    command = ADAPTER.build_command(
        project_root=tmp_path,
        result_path=tmp_path / "result.json",
        schema_path=tmp_path / "result.schema.json",
        model="gpt-5.6-terra",
        effort="high",
        resume_session_id="th-1",
    )

    assert command[:3] == ["codex", "exec", "resume"]
    assert "th-1" in command
