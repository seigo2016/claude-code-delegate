from __future__ import annotations

import json

import pytest

from conftest import PACKET, Workspace
from test_lifecycle import wait_for_terminal


@pytest.mark.parametrize("legacy", [False, True])
def test_write_completed_requires_known_complete(workspace: Workspace, legacy: bool) -> None:
    worker = workspace.worker_path
    mutation = 'good.pop("work_complete", None)' if legacy else 'good["work_complete"] = False'
    script = worker.read_text().replace(
        'event({"type": "item.completed",\n       "item": {"id": "i-2"',
        mutation + '\nevent({"type": "item.completed",\n       "item": {"id": "i-2"',
    )
    worker.write_text(script)
    packet = json.dumps({**json.loads(PACKET), "allowed_writes": ["allowed.txt"]})
    handle = workspace.submit(packet=packet, role="bounded-implementer")
    state = wait_for_terminal(workspace, handle["task_id"])
    if legacy:
        assert state["status"] == "unverified"
        result = workspace.run("collect", handle["task_id"])["result"]
        assert result["work_complete"] is None
        assert "unknown" in result["blockers"][-1]
    else:
        assert state["status"] == "failed"
        assert state["failure_class"] == "incomplete_work"


@pytest.mark.parametrize("terminal_already_emitted", [False, True])
def test_supervisor_exception_terminates_worker_and_emits_one_terminal_event(
    tmp_path, monkeypatch, terminal_already_emitted
):
    import subprocess
    import sys

    from delegate import events, runner, store
    from delegate.adapters.codex import CodexAdapter

    task_dir = tmp_path / "tasks" / "task-1"
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("prompt")
    state = {
        "task_id": "task-1",
        "status": "queued",
        "adapter": "codex",
        "lock_path": str(tmp_path / "worker.lock"),
        "project_root": str(tmp_path),
        "prompt_path": str(prompt),
        "result_path": str(tmp_path / "result.json"),
        "events_path": str(tmp_path / "worker-events.jsonl"),
        "stderr_path": str(tmp_path / "worker-stderr.txt"),
        "model": "test",
        "effort": "test",
        "writes_allowed": False,
        "runs_commands": False,
        "allowed_read_roots": [],
        "timeout_sec": 60,
    }
    store.write_json(task_dir / "state.json", state)
    adapter = CodexAdapter()
    monkeypatch.setattr(
        adapter,
        "build_command",
        lambda **kwargs: [sys.executable, "-c", "import time; time.sleep(60)"],
    )
    monkeypatch.setattr(runner.registry, "get", lambda name: adapter)
    monkeypatch.setattr(runner.signal, "signal", lambda *args: None)
    processes = []
    popen = subprocess.Popen

    def start_worker(*args, **kwargs):
        process = popen(*args, **kwargs)
        if kwargs.get("start_new_session"):
            processes.append(process)
        return process

    def fail_after_start(*args):
        assert processes and processes[0].poll() is None
        if terminal_already_emitted:
            events.emit(task_dir, "failed", failure_class="existing", failure_message="existing")
        raise RuntimeError("supervisor broke")

    monkeypatch.setattr(runner.subprocess, "Popen", start_worker)
    monkeypatch.setattr(runner, "_absorb", fail_after_start)
    try:
        assert runner.run(task_dir) == 1
        assert processes[0].poll() is not None
        records = [
            json.loads(line) for line in (task_dir / "events.jsonl").read_text().splitlines()
        ]
        terminal = [record for record in records if record["status"] in events.TERMINAL_STATES]
        assert len(terminal) == 1
        assert terminal[0]["failure_class"] == (
            "existing" if terminal_already_emitted else "RuntimeError"
        )
        assert terminal[0]["failure_message"] == (
            "existing" if terminal_already_emitted else "supervisor broke"
        )
    finally:
        for process in processes:
            runner._terminate(process)
            process.wait(timeout=5)
