"""Creating a task and handing it to a detached worker.

Everything the worker needs is on disk before the process starts, so the caller
can get its handle and stop thinking about the task.
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
from delegate.adapters import registry

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
    """The identical task that is still running, if there is one.

    Only a task still in flight counts. Matching finished ones too would answer a
    fresh question with an old answer, and the question is usually being asked
    again because something has changed since.
    """
    if not root.exists():
        return None
    for path in sorted(root.glob("*/state.json"), reverse=True):
        try:
            state = store.read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if state.get("fingerprint") == key and state.get("status") in events.ACTIVE_STATES:
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
) -> dict[str, Any]:
    packet_module.validate(value)
    if value["host_only"]:
        raise SubmitRefused("host_only work must stay in Claude Code", run_in="claude-code")

    plan = settings.plan(role, worker=worker)
    if plan.role.requires_allowed_writes and not value["allowed_writes"]:
        raise SubmitRefused(f"role {role} requires a non-empty allowed_writes")
    try:
        registry.get(plan.adapter)
    except KeyError as error:
        raise SubmitRefused(str(error)) from error

    root = task_root(project_root)
    root.mkdir(parents=True, exist_ok=True)
    key = fingerprint(project_root, role, title, value)
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
        # Decided here rather than at run time so that a finished task can still
        # answer what its worker was allowed to do.
        "writes_allowed": bool(value["allowed_writes"]),
        "runs_commands": plan.role.runs_commands,
        "timeout_sec": plan.timeout,
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
    # The supervisor reports itself running once it holds the lock. Saying so
    # here would leave a window where the task looks alive but nothing holds it.
    state = events.update_returning(task_dir, pid=worker_process.pid)
    return handle_of(state)
