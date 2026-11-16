from __future__ import annotations

import json
from pathlib import Path

from delegate import events, store


def make_task(tmp_path: Path, status: str = "queued") -> Path:
    task_dir = tmp_path / "tasks" / "task-1"
    store.write_json(task_dir / "state.json", {"task_id": "task-1", "status": status})
    return task_dir


def test_emit_records_the_event_in_the_task_log_only(tmp_path: Path) -> None:
    task_dir = make_task(tmp_path)

    events.emit(task_dir, "running", pid=4242)

    assert store.read_json(task_dir / "state.json")["pid"] == 4242
    recorded = [json.loads(line) for line in (task_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [event["status"] for event in recorded] == ["running"]
    assert not (task_dir.parent / "events.jsonl").exists()


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
    before = (task_dir / "events.jsonl").read_text(encoding="utf-8")

    events.update(task_dir, event_count=7)
    events.update(task_dir, event_count=9)

    assert store.read_json(task_dir / "state.json")["event_count"] == 9
    assert (task_dir / "events.jsonl").read_text(encoding="utf-8") == before


def test_a_progress_update_cannot_revive_a_finished_task(tmp_path: Path) -> None:
    task_dir = make_task(tmp_path)
    events.emit(task_dir, "completed")

    events.update(task_dir, event_count=3)

    assert "event_count" not in store.read_json(task_dir / "state.json")


def test_failure_transitions_always_record_one_line_details(tmp_path: Path) -> None:
    for status, reason in [
        ("failed", "launch_error"),
        ("timeout", "timeout"),
        ("degraded", "worker_gone_with_result"),
        ("orphaned", "worker_gone"),
    ]:
        task_dir = make_task(tmp_path / status)
        state = events.emit(task_dir, status, terminal_reason=reason)
        assert state["failure_class"] == reason
        assert state["failure_message"] == reason
        assert store.read_json(task_dir / "state.json")["failure_message"] == reason

    task_dir = make_task(tmp_path / "exception")
    state = events.emit(
        task_dir,
        "failed",
        terminal_reason="launch_error",
        launch_error="OSError: first line\nsecond line",
    )
    assert state["failure_message"] == "OSError: first line"


def test_claim_delivery_records_the_first_claim_and_refuses_the_second(tmp_path: Path) -> None:
    task_dir = make_task(tmp_path, "completed")

    state, claimed = events.claim_delivery(task_dir, delivered_to={"kind": "session", "id": "a"})
    again, claimed_again = events.claim_delivery(
        task_dir, delivered_to={"kind": "session", "id": "b"}
    )

    assert claimed is True and claimed_again is False
    assert state["delivered_at"] and state["delivered_to"] == {"kind": "session", "id": "a"}
    assert again["delivered_to"] == {"kind": "session", "id": "a"}
    assert again["delivered_at"] == state["delivered_at"]
    log = (task_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(log) == 1


def test_only_one_of_many_concurrent_claims_wins(tmp_path: Path) -> None:
    import threading

    task_dir = make_task(tmp_path, "completed")
    start = threading.Barrier(16)
    outcomes: list[bool] = []

    def claim() -> None:
        start.wait()
        outcomes.append(events.claim_delivery(task_dir)[1])

    threads = [threading.Thread(target=claim) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count(True) == 1
    assert len((task_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()) == 1
