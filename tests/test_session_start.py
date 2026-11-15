"""The session-start hook settles tasks whose worker is gone and prints nothing.

Its output is fed into the session at every start and compaction, so a listing
there would repeat every task the session ever submitted. Completion is announced
by the watch hook, and ``delegate reconcile`` prints the list on demand.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from delegate import store
from test_unreachable import SESSION, make_task


def state_of(task_dir: Path) -> dict:
    return json.loads((task_dir / "state.json").read_text())


def test_session_start_settles_gone_workers_and_prints_nothing(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    ledger = project / ".claude" / "logs" / "delegate"
    watch = tmp_path / "watch"
    orphan = ledger / "orphan"
    elsewhere = tmp_path / "elsewhere" / "degraded"
    foreign = ledger / "foreign"
    for task_dir in (orphan, elsewhere, foreign):
        make_task(task_dir, project, "running")
    (elsewhere / "result.json").write_text("{}", encoding="utf-8")
    state = state_of(foreign)
    state.update(submitter_session_id="sess-2", owner_id="sess-2")
    store.write_json(foreign / "state.json", state)
    # A task kept outside the project is reached through the session's pending watch.
    registry = watch / hashlib.sha256(SESSION.encode()).hexdigest() / "pending"
    store.write_json(
        registry / f"{hashlib.sha256(str(elsewhere).encode()).hexdigest()}.json",
        {"task_dir": str(elsewhere)},
    )

    def start(payload: dict) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "delegate", "_hook-reconcile", "--project-root", str(project)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env={**os.environ, "DELEGATE_WATCH_ROOT": str(watch)},
            cwd=project,
        )

    # Without a session id the hook owns nothing, so it touches nothing.
    result = start({})
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert state_of(orphan)["status"] == "running"

    result = start({"session_id": SESSION})
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert (state_of(orphan)["status"], state_of(orphan)["terminal_reason"]) == (
        "orphaned",
        "worker_gone",
    )
    assert (state_of(elsewhere)["status"], state_of(elsewhere)["terminal_reason"]) == (
        "degraded",
        "worker_gone_with_result",
    )
    assert state_of(foreign)["status"] == "running"
