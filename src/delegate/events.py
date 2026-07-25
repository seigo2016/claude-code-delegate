"""Task state transitions, recorded as an append-only log.

Every transition is written under an exclusive lock, and a task that has already
reached a terminal state never moves again. A late update from a worker that was
killed mid-write therefore cannot resurrect a finished task or overwrite the
reason it finished.
"""

from __future__ import annotations

import fcntl
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


def emit(task_dir: Path, status: str, **fields: Any) -> dict[str, Any]:
    """Record a transition to ``status`` and return the resulting state."""
    lock_path = task_dir / "state.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = store.read_json(_state_path(task_dir))
        previous = state.get("status")
        if previous in TERMINAL_STATES and status != previous:
            return state

        ts = now_iso()
        store.append_jsonl(
            task_dir / "events.jsonl",
            {"ts": ts, "task_id": state["task_id"], "status": status, **fields},
        )
        store.append_jsonl(
            task_dir.parent / "events.jsonl",
            {"ts": ts, "task_id": state["task_id"], "status": status, **fields},
        )
        updated = {**state, **fields, "status": status, "updated_at": ts}
        if status in TERMINAL_STATES and not updated.get("terminal_at"):
            updated["terminal_at"] = ts
        store.write_json(_state_path(task_dir), updated)
        return updated
