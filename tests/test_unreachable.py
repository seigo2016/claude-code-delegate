"""One task or registry entry the hooks cannot reach never stops them for the others.

A stale entry can point under a task root that is read-only or gone. The hook skips
that entry (and leaves it out of what it writes back), so its exit code and its
notices depend only on the tasks it can reach.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from delegate import store

SESSION = "sess-1"


def make_task(task_dir: Path, project: Path, status: str) -> None:
    store.write_json(
        task_dir / "state.json",
        {
            "task_id": task_dir.name,
            "status": status,
            "created_at": "2020-01-01T00:00:00+00:00",
            "project_root": str(project),
            "title": "t",
            "role": "r",
            "task_dir": str(task_dir),
            "lock_path": str(task_dir / "worker.lock"),
            "result_path": str(task_dir / "result.json"),
            "submitter_session_id": SESSION,
            "owner_kind": "session",
            "owner_id": SESSION,
            "terminal_at": "2020-01-01T00:00:01+00:00",
        },
    )


class Scene:
    def __init__(self, base: Path) -> None:
        self.project = base / "proj"
        self.good = self.project / ".claude" / "logs" / "delegate" / "good"
        self.stale = base / "ro" / "stale"
        self.watch = base / "watch"
        self.registry = self.watch / hashlib.sha256(SESSION.encode()).hexdigest()
        make_task(self.good, self.project, "failed")
        make_task(self.stale, self.project, "running")

    def pending(self, *task_dirs: Path) -> None:
        for task_dir in task_dirs:
            digest = hashlib.sha256(str(task_dir).encode()).hexdigest()
            store.write_json(
                self.registry / "pending" / f"{digest}.json", {"task_dir": str(task_dir)}
            )

    def group(self, *task_dirs: Path) -> None:
        store.write_json(
            self.registry / "g.json",
            {"session_id": SESSION, "watch_tasks": [{"task_dir": str(d)} for d in task_dirs]},
        )

    def run(self, hook: str, payload: dict) -> subprocess.CompletedProcess[str]:
        args = ["--project-root", str(self.project)] if hook == "_hook-reconcile" else []
        return subprocess.run(
            [sys.executable, "-m", "delegate", hook, *args],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env={**os.environ, "DELEGATE_WATCH_ROOT": str(self.watch), "DELEGATE_WATCH_SEC": "1"},
            cwd=self.project,
        )

    def post_tool_use(self) -> subprocess.CompletedProcess[str]:
        return self.run(
            "_hook-watch",
            {
                "session_id": SESSION,
                "hook_event_name": "PostToolUse",
                "tool_name": "Bash",
                "tool_response": {"stdout": ""},
            },
        )


@pytest.fixture
def scene(tmp_path: Path) -> Iterator[Scene]:
    value = Scene(tmp_path)
    (tmp_path / "ro").chmod(0o555)
    value.stale.chmod(0o555)
    yield value
    value.stale.chmod(0o755)
    (tmp_path / "ro").chmod(0o755)


def ready_ids(result: subprocess.CompletedProcess[str]) -> list[str]:
    return [item["task_id"] for item in json.loads(result.stdout)["ready"]]


def test_session_start_passes_a_stale_entry_quietly(scene: Scene) -> None:
    scene.pending(scene.stale)
    result = scene.run("_hook-reconcile", {"session_id": SESSION})
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_a_stale_pending_entry_does_not_stop_every_later_bash_call(scene: Scene) -> None:
    scene.pending(scene.stale)
    result = scene.post_tool_use()
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ready"] == []
    assert not list((scene.registry / "pending").glob("*.json"))


def test_a_stale_entry_does_not_hide_a_ready_task_from_the_hook(scene: Scene) -> None:
    scene.pending(scene.stale, scene.good)
    result = scene.post_tool_use()
    assert result.returncode == 2, result.stderr
    assert ready_ids(result) == ["good"]


def test_pruning_drops_an_unreachable_task_and_still_wakes(scene: Scene) -> None:
    scene.group(scene.good, scene.stale)
    result = scene.post_tool_use()
    assert result.returncode == 2, result.stderr
    assert ready_ids(result) == ["good"]
    kept = json.loads((scene.registry / "g.json").read_text())["watch_tasks"]
    assert kept == [{"task_dir": str(scene.good)}]
