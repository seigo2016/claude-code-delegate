"""Creating a task and handing it to a detached worker.

Everything the worker needs is on disk before the process starts, so the caller
can get its handle and stop thinking about the task.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from delegate import config, envelope, events, store
from delegate import packet as packet_module
from delegate.adapters import registry

SETTINGS_FILE = Path(".claude") / "delegate.toml"
TASK_ROOT = Path(".claude") / "logs" / "delegate"
CONTEXT_FILES = ("AGENTS.md", "CLAUDE.md")
_SHAPE = {
    "status": "completed | decision_needed | unverified",
    "observed_facts": ["what you saw, each with the path it came from"],
    "verified_comparisons": ["what you checked against what, and the outcome"],
    "artifact_paths": ["paths a reader should open"],
    "blockers": [{"reason": "environment | not_done", "text": "what stopped you"}],
    "decision_needed": "the question only the caller can answer, or null",
    "report": "full report text or null",
    "counts": [{"name": "count name", "count": 0}],
    "coverage": {"done": 0, "total": 0, "remaining": ["unfinished item"]},
    "work_complete": "boolean: whether all objective implementation is complete",
}
FORBIDDEN = (
    "destructive writes",
    "changes outside the stated write scope",
    "conclusions beyond the requested evidence",
)

#: Who a task answers to, or who is asking: ``(kind, id)``.
Owner = tuple[str, str | None]


class SubmitRefused(Exception):
    """The request will not start a worker, and why."""

    code = "submit_refused"

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.detail = detail


class WorkerUnhealthy(SubmitRefused):
    """The worker's last runs all failed for a reason that lies with the worker."""

    code = "worker_unhealthy"


def owner_of(state: dict[str, Any]) -> Owner:
    """Who a task answers to. State from before owners existed is a session task."""
    return (
        state.get("owner_kind", "session"),
        state.get("owner_id", state.get("submitter_session_id")),
    )


def may_act(state: dict[str, Any], caller: Owner) -> bool:
    """A bridge task answers only to its owner; a session task, to any session.

    The id is stored in plain text: this guards against mistakes, not a hostile caller.
    """
    kind, owner_id = owner_of(state)
    return caller == (kind, owner_id) if kind == "bridge" else caller[0] == "session"


#: How many consecutive same-class failures make a worker unhealthy.
UNHEALTHY_STREAK = 3
#: ``terminal_reason`` values that mean the worker could not be run at all. Their
#: ``failure_class`` is an exception name that varies, so the reason is what is compared.
BROKEN_REASONS = frozenset({"launch_error", "supervisor_error"})


def worker_fault(state: dict[str, Any]) -> str | None:
    """The class of a failure that says the worker is broken, not the task.

    Left out on purpose, because a retry can well succeed or the task is at fault:
    ``worker_api_error`` (any other provider error, including a bad request),
    ``provider_capacity`` and the timeout classes (transient), and ``nonzero_exit``,
    ``empty_result``, ``invalid_result``, ``incomplete_work`` and
    ``write_scope_violation`` (what this task did, not whether the worker starts).
    """
    if state.get("status") != "failed":
        return None
    if state.get("terminal_reason") in BROKEN_REASONS:
        return str(state["terminal_reason"])
    if state.get("failure_class") == "auth_failed":
        return "auth_failed"
    return None


def _all_states(root: Path) -> list[dict[str, Any]]:
    found = []
    for path in sorted(root.glob("*/state.json")):
        try:
            found.append(store.read_json(path))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return found


def unhealthy_streak(root: Path, worker: str) -> tuple[str, list[str]] | None:
    """``(class, task ids)`` when the worker's last three terminal tasks all failed alike."""
    finished = []
    for state in _all_states(root):
        if state.get("worker") == worker and state.get("status") in events.TERMINAL_STATES:
            try:
                finished.append((datetime.fromisoformat(str(state["terminal_at"])), state))
            except (KeyError, ValueError):
                continue
    finished.sort(key=lambda item: item[0], reverse=True)
    latest = [state for _, state in finished[:UNHEALTHY_STREAK]]
    classes = {worker_fault(state) for state in latest}
    if len(latest) < UNHEALTHY_STREAK or len(classes) != 1 or None in classes:
        return None
    return str(classes.pop()), [str(state["task_id"]) for state in latest]


def settings_path(project_root: Path) -> Path:
    return project_root / SETTINGS_FILE


def task_root(project_root: Path) -> Path:
    return project_root / TASK_ROOT


def entry_dir(entry: Any) -> Path | None:
    """The task directory a watch or handle entry names, if it names one.

    Entries written before ``task_dir`` existed carry ``project_root`` and
    ``task_id`` and mean a task under the project's default task root.
    """
    if not isinstance(entry, dict):
        return None
    if isinstance(entry.get("task_dir"), str):
        return Path(entry["task_dir"])
    if isinstance(entry.get("project_root"), str) and isinstance(entry.get("task_id"), str):
        return task_root(Path(entry["project_root"])) / entry["task_id"]
    return None


def watch_registry_dir(session_id: str) -> Path:
    root = Path(
        os.environ.get("DELEGATE_WATCH_ROOT")
        or Path.home() / ".claude" / "delegate" / "watch-sessions"
    )
    return root / hashlib.sha256(session_id.encode()).hexdigest()


def _pending_path(session_id: str, task_dir: Path) -> Path:
    digest = hashlib.sha256(str(task_dir).encode()).hexdigest()
    return watch_registry_dir(session_id) / "pending" / f"{digest}.json"


def register_completion_watch(task_dir: Path, session_id: str | None) -> None:
    """Make a submitted task discoverable even if a shell pipeline hides its handle."""
    if session_id:
        store.write_json(_pending_path(session_id, task_dir), {"task_dir": str(task_dir)})


def pending_completion_watches(session_id: str | None = None) -> list[Path]:
    """The task directories this session submitted and has not yet been told about."""
    if not session_id:
        return []
    session_root = watch_registry_dir(session_id) / "pending"
    legacy_root = watch_registry_dir(session_id).parent / "pending-submissions"
    found: list[Path] = []
    for path in [*sorted(session_root.glob("*.json")), *sorted(legacy_root.glob("*.json"))]:
        try:
            task_dir = entry_dir(store.read_json(path))
            if task_dir is None or owner_of(store.read_json(task_dir / "state.json")) != (
                "session",
                session_id,
            ):
                continue
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if path != _pending_path(session_id, task_dir):
            # Written by an older version, which named it differently.
            register_completion_watch(task_dir, session_id)
            path.unlink(missing_ok=True)
        found.append(task_dir)
    return found


def clear_pending_completion_watches(session_id: str, task_dirs: list[Path]) -> None:
    for task_dir in task_dirs:
        _pending_path(session_id, task_dir).unlink(missing_ok=True)


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
    lines.append(
        "You are a delegated worker, not the Chair, and must not delegate to other workers "
        "or run `delegate submit`, whatever repository instructions say about delegating."
    )
    lines.append("Read first:")
    lines.extend(f"- {item}" for item in value["read"] or ["- nothing specified"])
    if value.get("read_optional"):
        lines.append("Read these if they exist; they may be absent or be created by this task:")
        lines.extend(f"- {item}" for item in value["read_optional"])
    if plan.denied_read_paths:
        lines.append("Do not read these paths:")
        lines.extend(f"- {path}" for path in plan.denied_read_paths)
    lines.append("Write permissions:")
    if value["allowed_writes"]:
        lines.append(
            "Inside the repository, write only to these canonical repository-relative paths:"
        )
        lines.extend(f"- {item}" for item in value["allowed_writes"])
        lines.append("Do not write a similarly named file elsewhere.")
    else:
        lines.append("Do not write inside the repository.")
    if plan.role.allowed_write_roots:
        lines.append("External write roots allowed by this role:")
        lines.extend(f"- {root}" for root in plan.role.allowed_write_roots)
        lines.append(
            "Outside the repository, write only below these roots"
            + (" or $TMPDIR." if value["allowed_writes"] or plan.role.runs_commands else ".")
        )
    if value["allowed_writes"] or plan.role.runs_commands:
        lines.append("Create temporary files and directories only under $TMPDIR.")
    elif not plan.role.allowed_write_roots:
        lines.append(
            "This is a read-only task. Do not invoke write, edit, or patch tools; "
            "do not create files in .claude, /tmp, or elsewhere. "
            "Deliver tables and catalogs in the report field, not as files."
        )
    lines.append("Do not:")
    lines.extend(f"- {item}" for item in forbidden)
    if plan.common_instructions:
        lines.append("Instructions common to all roles:")
        lines.append(plan.common_instructions.strip())
    if plan.role.instructions:
        lines.append("Role instructions:")
        lines.append(plan.role.instructions.strip())
    lines.append("Bring back evidence for:")
    lines.extend(f"- {item}" for item in value["required_evidence"])
    if value["allowed_writes"]:
        lines.append(
            "Do not return until you have completed all implementation of the objective. "
            "If blocked, report the incomplete work and the blocker. "
            "After implementation is complete, return unverified "
            "only for verification that environmental constraints prevent you from running, "
            "and state that verification in blockers. Set work_complete to true only when all "
            "implementation is complete."
        )
    else:
        lines.append(
            "If you cannot run required verification or cannot supply its evidence, return "
            "unverified instead of completed and state what could not be verified in blockers."
        )
    lines.append(
        "Each blocker is an object with a reason and a text. The reason is "
        "environment when a tool, host or sandbox failure prevented the work or check, "
        "and not_done when planned work or verification is unfinished. "
        "A check the packet prohibits is not a blocker; report it in observed_facts or report."
    )
    lines.append("")
    # Only one backend can be handed a schema, so the shape is stated here for
    # all of them. Without it a worker invents its own and the run is wasted.
    lines.append("Reply with this JSON object and nothing else. No prose, no code fence:")
    fields = (*envelope.FIELDS, *envelope.OPTIONAL_FIELDS)
    if value["allowed_writes"]:
        fields = (*fields, "work_complete")
    lines.append(json.dumps({field: _SHAPE[field] for field in fields}, indent=2))
    lines.append("")
    lines.append(
        "The short list limits of five items and 300 characters apply only to what the Chair "
        "is shown. Put complete tables and details in report. "
        "Do not shrink the requested scope or return decision_needed because output is large. "
        "If you cannot finish, return coverage with done, total, and the remaining items listed. "
        "Use null for report, counts, or coverage when not needed; "
        "counts is an array of name and integer count objects."
    )
    lines.append("Include every listed key; do not include report_path (delegate sets it).")
    lines.append(
        "Do not include metadata keys (such as toolAction, toolSummary, reasoning) "
        "in the JSON object."
    )
    lines.append("Cite exact paths. Do not paste raw logs.")
    return "\n".join(lines) + "\n"


def _find_duplicate(root: Path, key: str, owner: Owner, *, fast: bool) -> dict[str, Any] | None:
    """The identical task that is still running, if there is one.

    Only a task still in flight counts. Matching finished ones too would answer a
    fresh question with an old answer, and the question is usually being asked
    again because something has changed since. The worker does not matter: the
    request is the same on either arm of a comparison.
    """
    for state in reversed(_all_states(root)):
        if (
            state.get("fingerprint") == key
            and state.get("status") in events.ACTIVE_STATES
            and owner_of(state) == owner
            and state.get("fast", False) == fast
        ):
            return state
    return None


def handle_of(state: dict[str, Any], *, deduplicated: bool = False) -> dict[str, Any]:
    return {
        "task_id": state["task_id"],
        "project_root": state["project_root"],
        "task_dir": state["task_dir"],
        "status": state["status"],
        "role": state["role"],
        "worker": state["worker"],
        "model": state["model"],
        "effort": state["effort"],
        "fast": state.get("fast", False),
        "deduplicated": deduplicated,
        "next_action": "end_turn",
        "completion_delivery": "async_rewake",
    }


def _non_repo_relative(values: list[str]) -> list[str]:
    invalid = []
    for value in values:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or value.startswith("~"):
            invalid.append(value)
    return invalid


def _external_reads(values: list[str]) -> list[str]:
    invalid_paths = set(_non_repo_relative(values))
    return [value for value in values if "://" in value or value in invalid_paths]


def _is_within_allowed_read_root(value: str, roots: tuple[str, ...]) -> bool:
    path = Path(value)
    if not path.is_absolute():
        return False
    resolved_path = path.resolve(strict=False)
    return any(resolved_path.is_relative_to(Path(root).resolve(strict=False)) for root in roots)


def _invalid_scoped_reads(values: list[str], role: config.Role) -> list[str]:
    invalid = []
    for value in values:
        if value in _external_reads([value]) and not _is_within_allowed_read_root(
            value, role.allowed_read_roots
        ):
            invalid.append(value)
    return invalid


def _denied_reads(
    project_root: Path, values: list[str], denied_read_paths: tuple[str, ...]
) -> list[dict[str, str]]:
    root = project_root.resolve(strict=False)
    resolved_denied = [(denied, Path(denied).resolve(strict=False)) for denied in denied_read_paths]
    matches = []
    for value in values:
        path = Path(value)
        resolved_read = (path if path.is_absolute() else root / path).resolve(strict=False)
        for denied, resolved_path in resolved_denied:
            if resolved_read.is_relative_to(resolved_path) or resolved_path.is_relative_to(
                resolved_read
            ):
                matches.append({"read": value, "denied_read_path": denied})
    return matches


def _preflight(plan: config.Plan, binary: str) -> None:
    """Refuse work that cannot possibly run, in seconds rather than minutes.

    Only checks that cannot be wrong are here: a missing binary, an empty
    model, a non-positive timeout, and a configured write root that does not
    exist. Everything else is the worker's to discover at run time.
    """
    if shutil.which(binary) is None:
        raise SubmitRefused(f"worker binary not on PATH: {binary}")
    if not plan.model:
        raise SubmitRefused(f"worker {plan.worker} defines no model for level {plan.role.level}")
    if plan.timeout <= 0:
        raise SubmitRefused(f"role {plan.role.name} has a non-positive timeout: {plan.timeout}")
    missing = [root for root in plan.role.allowed_write_roots if not Path(root).exists()]
    if missing:
        raise SubmitRefused(
            "allowed_write_roots do not exist",
            missing_allowed_write_roots=missing,
        )


def arm_for(task_id: str, arms: tuple[str, ...]) -> str:
    """The comparison arm a task takes, by a stable hash of its id."""
    return arms[hashlib.sha256(task_id.encode()).digest()[0] % len(arms)]


def _candidates(
    settings: config.Settings, role: str, worker: str | None, task_id: str
) -> tuple[list[str | None], str | None]:
    """The workers to try in order, and the route policy.

    An explicit worker always wins. A role that compares two workers tries the
    one its task id hashes to, then the other.
    """
    if worker is not None:
        return [worker], "explicit"
    role_def = settings.roles.get(role)
    if role_def is None or not role_def.compare_workers:
        return [None], None
    first = arm_for(task_id, role_def.compare_workers)
    return [first, *(arm for arm in role_def.compare_workers if arm != first)], "compare"


def _runnable(
    settings: config.Settings,
    role: str,
    worker: str | None,
    fast: bool | None,
    root: Path,
    *,
    allow_unhealthy: bool,
) -> tuple[config.Plan, dict[str, Any] | None]:
    """The plan for one worker and how its health was waived, or why it cannot run."""
    plan = settings.plan(role, worker=worker, fast=fast)
    override: dict[str, Any] | None = None
    streak = unhealthy_streak(root, plan.worker)
    if streak is not None:
        failure_class, task_ids = streak
        if not allow_unhealthy:
            raise WorkerUnhealthy(
                f"the last {UNHEALTHY_STREAK} tasks of {plan.worker} all failed with "
                f"{failure_class} ({', '.join(task_ids)}); fix the worker, or pass "
                "--allow-unhealthy to submit anyway",
                worker=plan.worker,
                failure_class=failure_class,
                task_ids=task_ids,
            )
        override = {"failure_class": failure_class, "task_ids": task_ids}
    try:
        adapter = registry.get(plan.adapter)
    except KeyError as error:
        raise SubmitRefused(str(error)) from error
    _preflight(plan, adapter.name)
    return plan, override


def submit(
    *,
    project_root: Path,
    settings: config.Settings,
    role: str,
    title: str,
    value: dict[str, Any],
    worker: str | None = None,
    fast: bool | None = None,
    caller: Owner = ("session", None),
    config_path: Path | None = None,
    config_sha256: str | None = None,
    ledger: Path | None = None,
    allow_unhealthy: bool = False,
) -> dict[str, Any]:
    """Create a task and start its worker.

    ``caller`` is who submits: a ``("bridge", id)`` task answers only to that id
    and no hook announces it. ``ledger`` is an absolute task root.
    """
    packet_module.validate(value)
    if value["host_only"]:
        raise SubmitRefused("host_only work must stay in Claude Code", run_in="claude-code")
    root = ledger if ledger is not None else task_root(project_root)
    task_id = str(uuid.uuid4())
    candidates, route_policy = _candidates(settings, role, worker, task_id)
    key = fingerprint(project_root, role, title, value)
    # An identical task still running is the answer, whatever the worker's health
    # is now: asking again must not be refused for the worker that is doing it.
    try:
        first = settings.plan(role, worker=candidates[0], fast=fast)
    except config.ConfigError:
        pass
    else:
        running = _find_duplicate(
            root,
            key,
            caller,
            fast=first.fast,
        )
        if running is not None:
            return handle_of(running, deduplicated=True)
    failures: list[Exception] = []
    for candidate in candidates:
        try:
            plan, override = _runnable(
                settings, role, candidate, fast, root, allow_unhealthy=allow_unhealthy
            )
            break
        except (SubmitRefused, config.ConfigError) as error:
            failures.append(error)
    else:
        raise failures[0]
    route_arm = plan.worker if route_policy == "compare" else None
    escalation_reason = value.get("escalation_reason")
    if plan.role.allowed_escalation_reasons:
        if escalation_reason is None:
            raise SubmitRefused(f"role {role} requires escalation_reason")
        if escalation_reason not in plan.role.allowed_escalation_reasons:
            raise SubmitRefused(
                f"escalation_reason {escalation_reason!r} is not allowed for role {role}",
                allowed_escalation_reasons=list(plan.role.allowed_escalation_reasons),
            )
    if plan.role.requires_explicit_user_direction and value.get("user_directed") is not True:
        raise SubmitRefused(f"role {role} requires user_directed: true")
    if plan.role.requires_allowed_writes and not value["allowed_writes"]:
        raise SubmitRefused(f"role {role} requires a non-empty allowed_writes")
    if plan.role.forbids_allowed_writes and value["allowed_writes"]:
        raise SubmitRefused(f"role {role} is read-only and forbids allowed_writes")

    invalid_writes = _non_repo_relative(value["allowed_writes"])
    if invalid_writes:
        raise SubmitRefused(
            "allowed_writes must contain canonical repository-relative paths",
            invalid_allowed_writes=invalid_writes,
        )
    denied_reads = _denied_reads(
        project_root, [*value["read"], *value.get("read_optional", [])], plan.denied_read_paths
    )
    if denied_reads:
        raise SubmitRefused(
            "packet reads overlap denied_read_paths",
            denied_reads=denied_reads,
        )
    if plan.role.repo_local_reads:
        invalid_reads = _invalid_scoped_reads(
            [*value["read"], *value.get("read_optional", [])], plan.role
        )
        if invalid_reads:
            raise SubmitRefused(
                f"role {role} accepts repository-local reads only, "
                "plus configured allowed_read_roots",
                invalid_reads=invalid_reads,
            )
    missing_reads = packet_module.missing_reads(value, project_root)
    if missing_reads:
        raise SubmitRefused(
            "packet reads do not exist; paths whose existence is itself the question, "
            "or that the worker will create, belong in read_optional",
            missing_reads=missing_reads,
        )

    root.mkdir(parents=True, exist_ok=True)
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
        "task_root": str(root),
        "config_path": str(config_path) if config_path is not None else None,
        "config_sha256": config_sha256,
        "role": role,
        "title": title,
        "worker": plan.worker,
        "route_arm": route_arm,
        "route_policy": route_policy,
        "unhealthy_override": override,
        "adapter": plan.adapter,
        "model": plan.model,
        "effort": plan.effort,
        "fast": plan.fast,
        "escalation_reason": escalation_reason,
        "user_directed": bool(value.get("user_directed", False)),
        # Decided here rather than at run time so that a finished task can still
        # answer what its worker was allowed to do.
        "writes_allowed": bool(value["allowed_writes"]),
        "runs_commands": plan.role.runs_commands,
        "temp_root": settings.temp_root,
        "allowed_read_roots": list(plan.role.allowed_read_roots),
        "allowed_write_roots": list(plan.role.allowed_write_roots),
        "timeout_sec": plan.timeout,
        "resume_limit": plan.resume_limit,
        "isolate_home": plan.isolate_home,
        "resume_count": 0,
        "resume_results": [],
        "pid": None,
        "session_id": None,
        "submitter_session_id": caller[1] if caller[0] == "session" else None,
        "owner_kind": caller[0],
        "owner_id": caller[1],
        "delivered_to": None,
        "exit_code": None,
        "terminal_reason": None,
        "failure_class": None,
        "failure_message": None,
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
    register_completion_watch(task_dir, state["submitter_session_id"])

    try:
        worker_process = subprocess.Popen(
            [sys.executable, "-m", "delegate", "_run", str(task_dir)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=project_root,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as error:
        state = events.emit(
            task_dir,
            "failed",
            terminal_reason="launch_error",
            failure_class=type(error).__name__,
            failure_message=str(error) or type(error).__name__,
        )
        return handle_of(state)
    # The supervisor reports itself running once it holds the lock. Saying so
    # here would leave a window where the task looks alive but nothing holds it.
    state = events.update_returning(task_dir, pid=worker_process.pid)
    return handle_of(state)
