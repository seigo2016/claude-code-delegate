"""What every adapter owes the broker.

The sample lines were captured from the actual CLIs, not invented.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from delegate.adapters import claude, codex, opencode
from delegate.adapters.base import WorkerAdapter

FINAL = json.dumps(
    {
        "status": "completed",
        "observed_facts": ["one"],
        "verified_comparisons": [],
        "artifact_paths": ["README.md"],
        "blockers": [],
        "decision_needed": None,
    }
)


@dataclass
class Sample:
    adapter: WorkerAdapter
    session_line: str
    session_id: str
    final_line: str
    tool_started_line: str | None


SAMPLES = {
    "codex": Sample(
        adapter=codex.CodexAdapter(),
        session_line=json.dumps({"type": "thread.started", "thread_id": "th-1"}),
        session_id="th-1",
        final_line=json.dumps(
            {
                "type": "item.completed",
                "item": {"id": "i-2", "type": "agent_message", "text": FINAL},
            }
        ),
        tool_started_line=json.dumps(
            {"type": "item.started", "item": {"id": "i-1", "type": "command_execution"}}
        ),
    ),
    "opencode": Sample(
        adapter=opencode.OpenCodeAdapter(),
        session_line=json.dumps(
            {
                "type": "step_start",
                "sessionID": "ses_065",
                "part": {"id": "prt_1", "type": "step-start"},
            }
        ),
        session_id="ses_065",
        final_line=json.dumps(
            {
                "type": "text",
                "sessionID": "ses_065",
                "part": {"id": "prt_2", "type": "text", "text": FINAL},
            }
        ),
        tool_started_line=json.dumps(
            {
                "type": "tool_use",
                "sessionID": "ses_065",
                "part": {
                    "id": "prt_3",
                    "type": "tool",
                    "tool": "read",
                    "state": {"status": "running"},
                },
            }
        ),
    ),
    "claude": Sample(
        adapter=claude.ClaudeAdapter(),
        session_line=json.dumps(
            {"type": "system", "subtype": "init", "session_id": "c8aae049", "model": "haiku"}
        ),
        session_id="c8aae049",
        final_line=json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": "c8aae049",
                "result": FINAL,
            }
        ),
        tool_started_line=None,
    ),
}


@pytest.fixture(params=sorted(SAMPLES), ids=sorted(SAMPLES))
def sample(request: pytest.FixtureRequest) -> Sample:
    return SAMPLES[request.param]


def test_junk_is_ignored_rather_than_crashing(sample: Sample) -> None:
    assert sample.adapter.parse_events("not json") == []
    assert sample.adapter.parse_events(json.dumps(["not", "an", "object"])) == []
    assert sample.adapter.parse_events(json.dumps({"type": "something.unheard.of"})) == []
    assert sample.adapter.parse_stderr_lines("nothing notable here") == []


def test_the_session_identity_is_surfaced(sample: Sample) -> None:
    (event,) = [
        event
        for event in sample.adapter.parse_events(sample.session_line)
        if event.kind == "session_started"
    ]

    assert event.session_id == sample.session_id


def test_the_final_answer_arrives_as_a_message_with_its_text(sample: Sample) -> None:
    events = sample.adapter.parse_events(sample.final_line)

    (message,) = [event for event in events if event.item_type == "agent_message"]
    assert message.text == FINAL


def test_an_unfinished_tool_call_is_visible_where_the_backend_reports_one(
    sample: Sample,
) -> None:
    if sample.tool_started_line is None:
        pytest.skip(f"{sample.adapter.name} does not report tool calls before they finish")

    (event,) = [
        event
        for event in sample.adapter.parse_events(sample.tool_started_line)
        if event.kind == "item_started"
    ]

    assert event.item_id and event.item_type


def test_the_command_carries_the_model_and_the_effort(sample: Sample, tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("do the thing", encoding="utf-8")

    command = sample.adapter.build_command(
        project_root=tmp_path,
        prompt_path=prompt,
        result_path=tmp_path / "result.json",
        schema_path=tmp_path / "result.schema.json",
        model="a-model",
        effort="high",
    )

    assert command[0] == sample.adapter.name
    joined = " ".join(command)
    assert "a-model" in joined
    assert "high" in joined
