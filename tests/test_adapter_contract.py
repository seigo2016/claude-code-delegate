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
    #: The run of arguments that appears only when the task declared no writes. None
    #: where the backend offers no per-run permission flag to say it with.
    read_only_flags: tuple[str, ...] | None


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
        read_only_flags=("--sandbox", "read-only"),
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
        read_only_flags=None,
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
        read_only_flags=("--disallowed-tools", "Edit", "Write", "NotebookEdit"),
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


def contains(command: list[str], run: tuple[str, ...]) -> bool:
    return any(tuple(command[i : i + len(run)]) == run for i in range(len(command)))


def build(
    sample: Sample, tmp_path: Path, *, writes_allowed: bool, runs_commands: bool = False
) -> list[str]:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("do the thing", encoding="utf-8")
    return sample.adapter.build_command(
        project_root=tmp_path,
        prompt_path=prompt,
        result_path=tmp_path / "result.json",
        schema_path=tmp_path / "result.schema.json",
        model="a-model",
        effort="high",
        writes_allowed=writes_allowed,
        runs_commands=runs_commands,
    )


def test_the_command_carries_the_model_and_the_effort(sample: Sample, tmp_path: Path) -> None:
    command = build(sample, tmp_path, writes_allowed=True)

    assert command[0] == sample.adapter.name
    joined = " ".join(command)
    assert "a-model" in joined
    assert "high" in joined


def test_a_task_that_declared_no_writes_is_told_so_in_the_command(
    sample: Sample, tmp_path: Path
) -> None:
    if sample.read_only_flags is None:
        pytest.skip(f"{sample.adapter.name} has no per-run permission flag to say it with")

    reading = build(sample, tmp_path, writes_allowed=False)
    writing = build(sample, tmp_path, writes_allowed=True)

    assert contains(reading, sample.read_only_flags)
    assert not contains(writing, sample.read_only_flags)


def test_a_role_that_runs_commands_gets_a_filesystem_it_can_write_to(
    sample: Sample, tmp_path: Path
) -> None:
    # Reads oddly on purpose: a task that declared no writes still gets a writable
    # filesystem here, because a test run needs one.
    if sample.adapter.name != "codex":
        pytest.skip(f"{sample.adapter.name} does not stand on a filesystem")

    command = build(sample, tmp_path, writes_allowed=False, runs_commands=True)

    assert contains(command, ("--sandbox", "workspace-write"))


def test_a_task_that_declared_writes_is_allowed_to_make_them(
    sample: Sample, tmp_path: Path
) -> None:
    # Being handed the editing tools was not enough: every write was still put to
    # someone who was not there, and the task failed having done the work.
    if sample.adapter.name != "claude":
        pytest.skip(f"{sample.adapter.name} has no tool allowance to give")

    command = build(sample, tmp_path, writes_allowed=True)

    assert contains(command, ("--allowedTools", "Edit", "Write", "NotebookEdit"))


def test_running_commands_does_not_buy_the_right_to_edit(sample: Sample, tmp_path: Path) -> None:
    if sample.adapter.name != "claude":
        pytest.skip(f"{sample.adapter.name} does not withhold tools")

    command = build(sample, tmp_path, writes_allowed=False, runs_commands=True)

    assert contains(command, ("--disallowed-tools", "Edit", "Write", "NotebookEdit"))


def test_a_run_that_was_refused_things_says_so(sample: Sample) -> None:
    # The run below succeeded. Refusals do not fail one, which is why they have to
    # be carried out of it.
    if sample.adapter.name != "claude":
        pytest.skip(f"{sample.adapter.name} does not report permission denials")

    line = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": "c8aae049",
            "result": FINAL,
            "permission_denials": [
                {"tool_name": "Bash", "tool_use_id": "t1"},
                {"tool_name": "Bash", "tool_use_id": "t2"},
            ],
        }
    )

    events = sample.adapter.parse_events(line)

    assert [e.text for e in events if e.kind == "runtime_warning"] == [
        "permission_denied",
        "permission_denied",
    ]


@pytest.mark.parametrize(
    ("adapter", "line"),
    [
        (
            codex.CodexAdapter(),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "i-3",
                        "type": "file_change",
                        "changes": [{"path": "/repo/changed.txt", "kind": "update"}],
                    },
                }
            ),
        ),
        (
            opencode.OpenCodeAdapter(),
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {
                        "id": "p-3",
                        "tool": "write",
                        "state": {
                            "status": "completed",
                            "input": {"filePath": "/repo/changed.txt"},
                        },
                    },
                }
            ),
        ),
        (
            claude.ClaudeAdapter(),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "t-3",
                                "name": "Write",
                                "input": {"file_path": "/repo/changed.txt"},
                            }
                        ]
                    },
                }
            ),
        ),
        (
            claude.ClaudeAdapter(),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "t-4",
                                "name": "NotebookEdit",
                                "input": {"notebook_path": "/repo/changed.txt"},
                            }
                        ]
                    },
                }
            ),
        ),
    ],
    ids=("codex", "opencode", "claude", "claude-notebook"),
)
def test_an_explicit_file_change_carries_its_path(adapter: WorkerAdapter, line: str) -> None:
    events = adapter.parse_events(line)

    assert [path for event in events for path in event.changed_paths] == ["/repo/changed.txt"]


def test_claude_reports_whether_a_tool_completed_successfully() -> None:
    line = json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t-3",
                        "is_error": True,
                    }
                ]
            },
        }
    )

    (event,) = claude.ClaudeAdapter().parse_events(line)

    assert event.succeeded is False
