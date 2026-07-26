"""A stand-in worker, so every failure mode can be produced on demand.

The real backends are CLI programs we cannot make fail to order. This fake one
speaks Codex's event format and does exactly what the mode file tells it, which
is what lets the failure taxonomy be tested rather than asserted.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

SETTINGS = """
default_worker = "codex"

[workers.codex]
adapter = "codex"
enabled = true
light = { model = "luna", effort = "high" }
standard = { model = "terra", effort = "high" }

[roles.artifact-auditor]
level = "standard"

[roles.bounded-implementer]
level = "standard"
requires_allowed_writes = true

[roles.verification-runner]
level = "standard"
runs_commands = true

[roles.impatient]
level = "standard"
timeout = 1
"""

FAKE_WORKER = r"""#!/usr/bin/env python3
import json, os, pathlib, sys, time

args = sys.argv[1:]
mode = pathlib.Path(os.environ["FAKE_MODE"]).read_text().strip()
pathlib.Path(os.environ["FAKE_CALLS"]).open("a").write(json.dumps(args) + "\n")

result_path = None
for i, arg in enumerate(args):
    if arg == "-o":
        result_path = pathlib.Path(args[i + 1])

def event(payload):
    print(json.dumps(payload), flush=True)

event({"type": "thread.started", "thread_id": "session-1"})

if mode == "tool_hang":
    event({"type": "item.started", "item": {"id": "i-1", "type": "command_execution"}})
    time.sleep(60)

if mode == "silent_hang":
    time.sleep(60)

good = {
    "status": "completed",
    "observed_facts": ["the fake worker ran"],
    "verified_comparisons": [],
    "artifact_paths": ["README.md"],
    "blockers": [],
    "decision_needed": None,
}

if mode == "finalization_hang":
    event({"type": "item.completed",
           "item": {"id": "i-2", "type": "agent_message", "text": json.dumps(good)}})
    time.sleep(60)

if mode == "nonzero_exit":
    sys.stderr.write("something broke\n")
    sys.exit(3)

if mode == "empty_result":
    event({"type": "turn.completed"})
    sys.exit(0)

if mode == "invalid_result":
    bad = {"status": "completed", "notes": "I did the thing"}
    event({"type": "item.completed",
           "item": {"id": "i-2", "type": "agent_message", "text": "I did the thing"}})
    result_path.write_text(json.dumps(bad))
    event({"type": "turn.completed"})
    sys.exit(0)

if mode == "result_in_message_only":
    event({"type": "item.completed",
           "item": {"id": "i-2", "type": "agent_message", "text": json.dumps(good)}})
    event({"type": "turn.completed"})
    sys.exit(0)

if mode == "writes_allowed":
    (pathlib.Path(os.environ["FAKE_CWD"]) / "allowed.txt").write_text("ok")

if mode == "writes_elsewhere":
    (pathlib.Path(os.environ["FAKE_CWD"]) / "unauthorized.txt").write_text("oops")

if mode == "decision_needed":
    good = {**good, "status": "decision_needed", "decision_needed": "pick a threshold"}

event({"type": "item.completed",
       "item": {"id": "i-2", "type": "agent_message", "text": json.dumps(good)}})
result_path.write_text(json.dumps(good))
event({"type": "turn.completed"})
sys.exit(0)
"""


PACKET = json.dumps(
    {
        "objective": "Count the tests.",
        "read": ["tests"],
        "allowed_writes": [],
        "required_evidence": ["test count"],
        "host_only": False,
    }
)


@dataclass
class Workspace:
    repo: Path
    settings: Path
    mode_file: Path
    calls_file: Path
    env: dict[str, str]

    worker_path: Path

    def remove_worker(self) -> None:
        """Leave the backend unlaunchable, without falling through to a real one."""
        self.worker_path.unlink()
        self.env["PATH"] = str(self.worker_path.parent)

    def mode(self, value: str) -> None:
        self.mode_file.write_text(value, encoding="utf-8")

    def calls(self) -> list[list[str]]:
        if not self.calls_file.exists():
            return []
        return [
            json.loads(line) for line in self.calls_file.read_text(encoding="utf-8").splitlines()
        ]

    def submit(
        self,
        *extra: str,
        packet: str = PACKET,
        role: str = "artifact-auditor",
        title: str = "count-tests",
        expect_success: bool = False,
    ) -> dict:
        (self.repo / "packet.json").write_text(packet, encoding="utf-8")
        return self.run(
            "submit",
            "--role",
            role,
            "--title",
            title,
            "--packet",
            "packet.json",
            *extra,
            expect_success=expect_success,
        )

    def run(self, *args: str, expect_success: bool = True) -> dict:
        completed = subprocess.run(
            [sys.executable, "-m", "delegate", *args],
            capture_output=True,
            text=True,
            env=self.env,
            cwd=self.repo,
        )
        if expect_success and completed.returncode != 0:
            raise AssertionError(f"delegate {args} failed: {completed.stdout}{completed.stderr}")
        return json.loads(completed.stdout or "{}")


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    settings = repo / ".claude" / "delegate.toml"
    settings.write_text(SETTINGS, encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    worker = fake_bin / "codex"
    worker.write_text(FAKE_WORKER, encoding="utf-8")
    worker.chmod(0o755)

    mode_file = tmp_path / "mode"
    mode_file.write_text("success", encoding="utf-8")
    calls_file = tmp_path / "calls.jsonl"

    src = str(Path(__file__).resolve().parents[1] / "src")
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "PYTHONPATH": src,
        "FAKE_MODE": str(mode_file),
        "FAKE_CALLS": str(calls_file),
        "FAKE_CWD": str(repo),
        "DELEGATE_HEARTBEAT_SEC": "0.05",
    }
    return Workspace(
        repo=repo,
        settings=settings,
        mode_file=mode_file,
        calls_file=calls_file,
        env=env,
        worker_path=worker,
    )
