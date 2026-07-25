from __future__ import annotations

import json
from pathlib import Path

from delegate import events, store


def make_task(tmp_path: Path, status: str = "queued") -> Path:
    task_dir = tmp_path / "tasks" / "task-1"
    store.write_json(task_dir / "state.json", {"task_id": "task-1", "status": status})
    return task_dir


def test_emit_records_the_event_in_both_the_task_and_the_shared_log(tmp_path: Path) -> None:
    task_dir = make_task(tmp_path)

    events.emit(task_dir, "running", pid=4242)

    assert store.read_json(task_dir / "state.json")["pid"] == 4242
    for log in (task_dir / "events.jsonl", task_dir.parent / "events.jsonl"):
        recorded = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        assert [event["status"] for event in recorded] == ["running"]


def test_a_terminal_state_can_never_be_overwritten_by_a_later_update(tmp_path: Path) -> None:
    task_dir = make_task(tmp_path)
    events.emit(task_dir, "completed")

    events.emit(task_dir, "running", pid=99)

    state = store.read_json(task_dir / "state.json")
    assert state["status"] == "completed"
    assert "pid" not in state


def test_progress_updates_change_the_state_without_filling_the_logs(tmp_path: Path) -> None:
    task_dir = make_task(tmp_path, status="running")
    events.emit(task_dir, "running")
    before = (task_dir.parent / "events.jsonl").read_text(encoding="utf-8")

    events.update(task_dir, event_count=7)
    events.update(task_dir, event_count=9)

    assert store.read_json(task_dir / "state.json")["event_count"] == 9
    assert (task_dir.parent / "events.jsonl").read_text(encoding="utf-8") == before


def test_a_progress_update_cannot_revive_a_finished_task(tmp_path: Path) -> None:
    task_dir = make_task(tmp_path)
    events.emit(task_dir, "completed")

    events.update(task_dir, event_count=3)

    assert "event_count" not in store.read_json(task_dir / "state.json")
