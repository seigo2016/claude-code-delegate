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
