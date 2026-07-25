"""Task state transitions, recorded as an append-only log.

A task that reached a terminal state never moves again, so a late write from a
dying worker cannot resurrect it or overwrite why it finished.
"""

from __future__ import annotations

import contextlib
import fcntl
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from delegate import store

TERMINAL_STATES = frozenset(
    {
        "completed",
        "decision_needed",
        "failed",
        "timeout",
        "cancelled",
        "orphaned",
        "degraded",
    }
)
ACTIVE_STATES = frozenset({"queued", "starting", "running", "cancellation_requested"})


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _state_path(task_dir: Path) -> Path:
    return task_dir / "state.json"


@contextlib.contextmanager
def _held(task_dir: Path) -> Iterator[dict[str, Any]]:
    lock_path = task_dir / "state.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield store.read_json(_state_path(task_dir))


def emit(task_dir: Path, status: str, **fields: Any) -> dict[str, Any]:
    with _held(task_dir) as state:
        previous = state.get("status")
        if previous in TERMINAL_STATES and status != previous:
            return state

        ts = now_iso()
        record = {"ts": ts, "task_id": state["task_id"], "status": status, **fields}
        store.append_jsonl(task_dir / "events.jsonl", record)
        store.append_jsonl(task_dir.parent / "events.jsonl", record)
        updated = {**state, **fields, "status": status, "updated_at": ts}
        if status in TERMINAL_STATES and not updated.get("terminal_at"):
            updated["terminal_at"] = ts
        store.write_json(_state_path(task_dir), updated)
        return updated


def update(task_dir: Path, **fields: Any) -> None:
    """Record progress without logging it.

    A run reports every few seconds. Logging that would bury the transitions the
    log exists for, and would grow the shared log without bound.
    """
    with _held(task_dir) as state:
        if state.get("status") in TERMINAL_STATES:
            return
        store.write_json(_state_path(task_dir), {**state, **fields, "updated_at": now_iso()})
