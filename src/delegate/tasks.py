"""Creating a task and handing it to a detached worker.

Submitting writes everything the worker needs to disk first, then starts a
process that outlives the caller. The caller gets a handle back immediately and
is expected to stop thinking about the task until it is told the task finished.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from delegate import config, envelope, events, store
from delegate import packet as packet_module

SETTINGS_FILE = Path(".claude") / "delegate.toml"
TASK_ROOT = Path(".claude") / "logs" / "delegate"
CONTEXT_FILES = ("AGENTS.md", "CLAUDE.md")
_SHAPE = {
    "status": "completed | decision_needed",
    "observed_facts": ["what you saw, each with the path it came from"],
    "verified_comparisons": ["what you checked against what, and the outcome"],
    "artifact_paths": ["paths a reader should open"],
    "blockers": ["what stopped you, if anything"],
    "decision_needed": "the question only the caller can answer, or null",
}
FORBIDDEN = (
    "destructive writes",
    "changes outside the stated write scope",
    "conclusions beyond the requested evidence",
)


class SubmitRefused(Exception):
    """The request will not start a worker, and why."""

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.detail = detail


def settings_path(project_root: Path) -> Path:
    return project_root / SETTINGS_FILE


def task_root(project_root: Path) -> Path:
    return project_root / TASK_ROOT


def fingerprint(project_root: Path, role: str, title: str, value: dict[str, Any]) -> str:
    """A stable identity for this exact request, so a repeat submit is not a repeat run."""
    canonical = json.dumps(
        {"project_root": str(project_root), "role": role, "title": title, "packet": value},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def compose_prompt(project_root: Path, plan: config.Plan, value: dict[str, Any]) -> str:
    forbidden = list(dict.fromkeys([*FORBIDDEN, *value.get("forbidden", [])]))
    context = [name for name in CONTEXT_FILES if (project_root / name).exists()]
    lines = [
        f"Project root: {project_root}",
        f"Role: {plan.role.name}",
        f"Task class: {plan.role.task_class}",
        f"Objective: {value['objective']}",
    ]
    if context:
        lines.append("Repository conventions (read and obey):")
        lines.extend(f"- {name}" for name in context)
    lines.append("Read first:")
    lines.extend(f"- {item}" for item in value["read"] or ["- nothing specified"])
    lines.append("You may write only:")
    lines.extend(f"- {item}" for item in value["allowed_writes"] or ["nothing"])
    lines.append("Do not:")
    lines.extend(f"- {item}" for item in forbidden)
    lines.append("Bring back evidence for:")
    lines.extend(f"- {item}" for item in value["required_evidence"])
    lines.append("")
    # Only one backend can be handed a schema, so the shape is stated here for
    # all of them. Without it a worker invents its own and the run is wasted.
    lines.append("Reply with this JSON object and nothing else. No prose, no code fence:")
    lines.append(json.dumps({field: _SHAPE[field] for field in envelope.FIELDS}, indent=2))
    lines.append("")
    lines.append(
        f"Each list holds at most {envelope.MAX_ITEMS} strings of "
        f"{envelope.MAX_ITEM_CHARS} characters or fewer. If the answer needs more than "
        "that, write it to a file you were allowed to write and return its path."
    )
    lines.append("Cite exact paths. Do not paste raw logs.")
    return "\n".join(lines) + "\n"


def _find_duplicate(root: Path, key: str) -> dict[str, Any] | None:
    if not root.exists():
        return None
    for path in sorted(root.glob("*/state.json"), reverse=True):
        try:
            state = store.read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if state.get("fingerprint") == key:
            return state
    return None


def handle_of(state: dict[str, Any], *, deduplicated: bool = False) -> dict[str, Any]:
    return {
        "task_id": state["task_id"],
        "status": state["status"],
        "role": state["role"],
        "worker": state["worker"],
        "model": state["model"],
        "effort": state["effort"],
        "state_path": state["state_path"],
        "result_path": state["result_path"],
        "deduplicated": deduplicated,
    }


def submit(
    *,
    project_root: Path,
    settings: config.Settings,
    role: str,
    title: str,
    value: dict[str, Any],
    worker: str | None = None,
    effort: str | None = None,
    timeout: int | None = None,
    fresh: bool = False,
) -> dict[str, Any]:
    packet_module.validate(value)
    refusal = packet_module.refusal(value)
    if refusal is not None:
        message, detail = refusal
        raise SubmitRefused(message, **detail)

    plan = settings.plan(role, worker=worker, effort=effort)
    if plan.role.requires_allowed_writes and not value["allowed_writes"]:
        raise SubmitRefused(f"role {role} requires a non-empty allowed_writes")

    root = task_root(project_root)
    root.mkdir(parents=True, exist_ok=True)
    key = fingerprint(project_root, role, title, value)
    if not fresh:
        duplicate = _find_duplicate(root, key)
        if duplicate is not None:
            return handle_of(duplicate, deduplicated=True)

    task_id = str(uuid.uuid4())
    task_dir = root / task_id
    task_dir.mkdir(mode=0o700)
    created = events.now_iso()
    state: dict[str, Any] = {
        "task_id": task_id,
        "fingerprint": key,
        "status": "queued",
        "created_at": created,
        "updated_at": created,
        "project_root": str(project_root),
        "role": role,
        "title": title,
        "worker": plan.worker,
        "adapter": plan.adapter,
        "model": plan.model,
        "effort": plan.effort,
        "task_class": plan.role.task_class,
        "timeout_sec": timeout if timeout is not None else plan.timeout,
        "pid": None,
        "session_id": None,
        "exit_code": None,
        "terminal_reason": None,
        "failure_class": None,
        "recovery_status": None,
        "recovered_result_path": None,
        "last_agent_message_path": None,
        "delivered_at": None,
        "heartbeat_at": None,
        "duration_sec": None,
        "event_count": 0,
        "task_dir": str(task_dir),
        "state_path": str(task_dir / "state.json"),
        "packet_path": str(task_dir / "packet.json"),
        "prompt_path": str(task_dir / "prompt.md"),
        "result_path": str(task_dir / "result.json"),
        "events_path": str(task_dir / "worker-events.jsonl"),
        "stderr_path": str(task_dir / "stderr.log"),
        "lock_path": str(task_dir / "worker.lock"),
    }
    store.write_json(task_dir / "packet.json", value)
    (task_dir / "prompt.md").write_text(compose_prompt(project_root, plan, value), encoding="utf-8")
    store.write_json(task_dir / "state.json", state)
    state = events.emit(task_dir, "starting")

    worker_process = subprocess.Popen(
        [sys.executable, "-m", "delegate", "_run", str(task_dir)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=project_root,
        start_new_session=True,
        close_fds=True,
    )
    state = events.emit(task_dir, "running", pid=worker_process.pid, started_at=events.now_iso())
    return handle_of(state)
