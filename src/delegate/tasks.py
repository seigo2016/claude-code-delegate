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
from dataclasses import dataclass
from datetime import datetime, timedelta
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
    "blockers": [
        {
            "reason": "forbidden_by_packet | environment | not_done",
            "text": "what stopped you",
            "forbidden_item": "the exact forbidden entry for forbidden_by_packet, else null",
        }
    ],
    "decision_needed": "the question only the caller can answer, or null",
    "report": "full report text or null",
    "counts": [{"name": "count name", "count": 0}],
    "coverage": {"done": 0, "total": 0, "remaining": ["unfinished item"]},
    "work_complete": "boolean: whether all objective implementation is complete",
}
FORBIDDEN = envelope.BUILT_IN_FORBIDDEN


class SubmitRefused(Exception):
    """The request will not start a worker, and why."""

    exit_code = 1

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.detail = detail


class WorkerUnhealthy(SubmitRefused):
    """The worker's last runs all failed for a reason that lies with the worker."""

    exit_code = 7


#: How many consecutive same-class failures make a worker unhealthy.
UNHEALTHY_STREAK = 3
#: ``failure_class`` values naming a credential the worker cannot use. ``auth_failed``
#: is what the adapters produce (a 401/403, an authentication error type, or an
#: ``api_key``/``auth`` code); the others are the names a hand-written or future
#: adapter would likely give the same thing.
AUTH_CLASSES = frozenset({"auth_failed", "auth_error", "api_key_expired"})
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
    reason = state.get("terminal_reason")
    if reason in BROKEN_REASONS:
        return str(reason)
    failure_class = state.get("failure_class")
    if failure_class in AUTH_CLASSES:
        return str(failure_class)
    return None


def _terminal_key(state: dict[str, Any]) -> tuple[datetime, str]:
    return (datetime.fromisoformat(str(state["terminal_at"])), str(state.get("task_id")))


@dataclass(frozen=True)
class Health:
    """A worker's unhealthy streak and when it may be tried again."""

    failure_class: str
    task_ids: list[str]
    last_failure_at: datetime
    until: datetime

    def probing(self, now: datetime) -> bool:
        """True once the cooldown is over: the next submit is a probe, not a retry."""
        return now >= self.until


def health_of(root: Path, worker: str, cooldown_hours: float) -> Health | None:
    """The worker's unhealthy streak, when its last three terminal tasks all failed alike.

    Newest first. A task of another outcome in between, or fewer than three tasks,
    means the worker is not known to be broken. The cooldown runs from the last
    failure of the streak, so a probe that fails again opens a new one.
    """
    if not root.exists():
        return None
    finished = []
    for state in _all_states(root):
        if state.get("worker") != worker or state.get("status") not in events.TERMINAL_STATES:
            continue
        try:
            finished.append((_terminal_key(state), state))
        except (KeyError, ValueError):
            continue
    finished.sort(key=lambda item: item[0], reverse=True)
    latest = [(key, state) for key, state in finished[:UNHEALTHY_STREAK]]
    if len(latest) < UNHEALTHY_STREAK:
        return None
    classes = {worker_fault(state) for _, state in latest}
    if len(classes) != 1 or None in classes:
        return None
    last = latest[0][0][0]
    return Health(
        failure_class=str(classes.pop()),
        task_ids=[str(state["task_id"]) for _, state in latest],
        last_failure_at=last,
        until=last + timedelta(hours=cooldown_hours),
    )


def recent_unhealthy(root: Path, worker: str) -> tuple[str, list[str]] | None:
    """``(class, task ids)`` of the worker's unhealthy streak, ignoring any cooldown."""
    health = health_of(root, worker, 0)
    return None if health is None else (health.failure_class, health.task_ids)


def settings_path(project_root: Path) -> Path:
    return project_root / SETTINGS_FILE


def task_root(project_root: Path) -> Path:
    return project_root / TASK_ROOT


def explicit_root(project_root: Path, root: str | Path | None) -> Path | None:
    """The task root a handle names, or ``None`` when it is the project's default.

    Handles, registry entries and watch groups written before ``--task-root`` existed
    carry no root, and must keep meaning the default one.
    """
    if not isinstance(root, str | Path) or not str(root):
        return None
    path = Path(root)
    return None if path == task_root(project_root) else path


def _completion_watch_registry_root() -> Path:
    configured = os.environ.get("DELEGATE_WATCH_ROOT")
    if configured:
        return Path(configured)
    return Path.home() / ".claude" / "delegate" / "watch-sessions"


def _completion_watch_path(
    session_id: str, project_root: Path, task_id: str, root: Path | None = None
) -> Path:
    identity = f"{project_root}\0{task_id}".encode()
    if root is not None:
        identity = f"{project_root}\0{task_id}\0{root}".encode()
    return (
        _completion_watch_registry_root()
        / hashlib.sha256(session_id.encode()).hexdigest()
        / "pending"
        / (hashlib.sha256(identity).hexdigest() + ".json")
    )


def register_completion_watch(
    project_root: Path, task_id: str, session_id: str | None, root: Path | None = None
) -> None:
    """Make a submitted task discoverable even if a shell pipeline hides its handle."""
    if not session_id:
        return
    entry = {"project_root": str(project_root), "task_id": task_id}
    if root is not None:
        entry["task_root"] = str(root)
    store.write_json(_completion_watch_path(session_id, project_root, task_id, root), entry)


def pending_completion_watches(session_id: str | None = None) -> list[dict[str, str]]:
    if not session_id:
        return []
    root = _completion_watch_registry_root()
    found: list[dict[str, str]] = []
    session_root = root / hashlib.sha256(session_id.encode()).hexdigest() / "pending"
    legacy_root = root / "pending-submissions"
    for path in [*sorted(session_root.glob("*.json")), *sorted(legacy_root.glob("*.json"))]:
        try:
            value = store.read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(value.get("project_root"), str) and isinstance(value.get("task_id"), str):
            project = Path(value["project_root"])
            root = explicit_root(project, value.get("task_root"))
            try:
                state = store.read_json(
                    (root or task_root(project)) / value["task_id"] / "state.json"
                )
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            owner = state.get("submitter_session_id")
            if owner != session_id:
                continue
            if path.parent == legacy_root:
                destination = _completion_watch_path(session_id, project, value["task_id"], root)
                store.write_json(destination, value)
                path.unlink(missing_ok=True)
            entry = {"project_root": value["project_root"], "task_id": value["task_id"]}
            if root is not None:
                entry["task_root"] = str(root)
            found.append(entry)
    return found


def clear_pending_completion_watches(session_id: str, watches: list[dict[str, str]]) -> None:
    if not session_id:
        return
    for watch in watches:
        project = Path(watch["project_root"])
        root = explicit_root(project, watch.get("task_root"))
        _completion_watch_path(session_id, project, watch["task_id"], root).unlink(missing_ok=True)


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
    own_forbidden = [item for item in value.get("forbidden", []) if item not in FORBIDDEN]
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
    lines.append("Do not (always):")
    lines.extend(f"- {item}" for item in FORBIDDEN)
    if own_forbidden:
        lines.append("The packet's forbidden list (do not):")
        lines.extend(f"- {item}" for item in own_forbidden)
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
        "not_done when planned work or verification is unfinished, and "
        "forbidden_by_packet when the packet itself prohibited a check. "
        "A forbidden_by_packet blocker must also carry forbidden_item, set to the exact "
        "string of the entry in the packet's forbidden list that prohibited the check "
        "(the entries under 'Do not (always)' are not part of that list); "
        "without an exact match it is treated as not_done. Otherwise forbidden_item is null."
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


def _find_duplicate(
    root: Path,
    key: str,
    owner_kind: str,
    owner_id: str | None,
    *,
    fast: bool = False,
    route_arm: str | None = None,
) -> dict[str, Any] | None:
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
        if (
            state.get("fingerprint") == key
            and state.get("status") in events.ACTIVE_STATES
            and state.get("owner_kind", "session") == owner_kind
            and state.get("owner_id", state.get("submitter_session_id")) == owner_id
            and state.get("fast", False) == fast
            # Two arms of a comparison are two runs, however alike their requests.
            and state.get("route_arm") == route_arm
        ):
            return state
    return None


def handle_of(state: dict[str, Any], *, deduplicated: bool = False) -> dict[str, Any]:
    handle = {
        "task_id": state["task_id"],
        "project_root": state["project_root"],
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
    # Only a ledger away from the default is named, so a default handle stays as it was.
    root = explicit_root(Path(state["project_root"]), state.get("task_root"))
    if root is not None:
        handle["task_root"] = str(root)
    if state.get("health_probe"):
        handle["health_probe"] = True
    # A comparison that could not use the arm it chose says so where the caller looks.
    if state.get("route_fallback_from"):
        handle["route_fallback"] = {
            "from": state["route_fallback_from"],
            "to": state["worker"],
            "reason": state.get("route_fallback_reason"),
        }
    return handle


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


def _route(
    settings: config.Settings, role: str, worker: str | None, root: Path
) -> tuple[str | None, str | None, str | None]:
    """Which worker to ask for, and why: ``(worker, route_arm, route_policy)``.

    An explicit worker always wins. Otherwise a role that compares workers takes
    them in turn, counted over the tasks of that role already in the ledger.
    """
    if worker is not None:
        return worker, None, "explicit"
    role_def = settings.roles.get(role)
    if role_def is None or not role_def.compare_workers:
        return None, None, None
    arms = role_def.compare_workers
    taken = sum(
        1 for state in _all_states(root) if state.get("role") == role and state.get("route_arm")
    )
    arm = arms[taken % len(arms)]
    return arm, arm, "compare"


def _all_states(root: Path) -> list[dict[str, Any]]:
    found = []
    for path in sorted(root.glob("*/state.json")):
        try:
            found.append(store.read_json(path))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return found


def _candidates(
    settings: config.Settings, role: str, chosen: str | None, route_policy: str | None
) -> list[str | None]:
    """The workers to try, in order: the one chosen, then (when comparing) the other arms."""
    role_def = settings.roles.get(role)
    if route_policy != "compare" or role_def is None or chosen not in role_def.compare_workers:
        return [chosen]
    arms = role_def.compare_workers
    start = arms.index(str(chosen))
    return [arms[(start + step) % len(arms)] for step in range(len(arms))]


@dataclass(frozen=True)
class _Arm:
    plan: config.Plan
    override: dict[str, Any] | None
    probe: bool


def _try_worker(
    settings: config.Settings,
    role: str,
    worker: str | None,
    fast: bool | None,
    ledger_root: Path,
    *,
    allow_unhealthy: bool,
    preflight: bool,
) -> _Arm:
    """The plan for one worker, how its health was waived if it was, or why it cannot run."""
    plan = settings.plan(role, worker=worker, fast=fast)
    override: dict[str, Any] | None = None
    probe = False
    health = health_of(ledger_root, plan.worker, settings.worker_health_cooldown_hours)
    if health is not None and allow_unhealthy:
        override = {"failure_class": health.failure_class, "task_ids": health.task_ids}
    elif health is not None and health.probing(datetime.now().astimezone()):
        probe = True
    elif health is not None:
        until = health.until.isoformat(timespec="seconds")
        raise WorkerUnhealthy(
            "worker_unhealthy",
            worker=plan.worker,
            failure_class=health.failure_class,
            task_ids=health.task_ids,
            cooldown_until=until,
            reason=(
                f"the last {UNHEALTHY_STREAK} tasks of {plan.worker} all failed with "
                f"{health.failure_class}; it is not tried again until {until} (cooldown "
                f"{settings.worker_health_cooldown_hours:g} h after the last failure) - "
                "wait for that, fix the worker, or pass --allow-unhealthy to retry now"
            ),
        )
    if preflight:
        try:
            adapter = registry.get(plan.adapter)
        except KeyError as error:
            raise SubmitRefused(str(error)) from error
        _preflight(plan, adapter.name)
    return _Arm(plan, override, probe)


def _reason(error: Exception) -> str:
    detail = getattr(error, "detail", None)
    if isinstance(detail, dict) and isinstance(detail.get("reason"), str):
        return str(detail["reason"])
    return str(error)


def submit(
    *,
    project_root: Path,
    settings: config.Settings,
    role: str,
    title: str,
    value: dict[str, Any],
    worker: str | None = None,
    fast: bool | None = None,
    owner: str | None = None,
    config_path: Path | None = None,
    config_sha256: str | None = None,
    ledger: Path | None = None,
    allow_unhealthy: bool = False,
) -> dict[str, Any]:
    packet_module.validate(value)
    if value["host_only"]:
        raise SubmitRefused("host_only work must stay in Claude Code", run_in="claude-code")

    ledger_root = ledger if ledger is not None else task_root(project_root)
    chosen, route_arm, route_policy = _route(settings, role, worker, ledger_root)
    candidates = _candidates(settings, role, chosen, route_policy)
    failures: list[tuple[str | None, Exception]] = []
    arm: _Arm | None = None
    for candidate in candidates:
        try:
            # With several arms to choose from, an arm that could not be started
            # is skipped; with one, the checks below say what is wrong as before.
            arm = _try_worker(
                settings,
                role,
                candidate,
                fast,
                ledger_root,
                allow_unhealthy=allow_unhealthy,
                preflight=len(candidates) > 1,
            )
        except (SubmitRefused, config.ConfigError) as error:
            failures.append((candidate, error))
            continue
        break
    if arm is None:
        first = failures[0][1]
        if isinstance(first, SubmitRefused) and len(failures) > 1:
            first.detail["other_arms"] = [
                {"worker": name, "reason": _reason(error)} for name, error in failures[1:]
            ]
        raise first
    plan, override, health_probe = arm.plan, arm.override, arm.probe
    fallback_from: str | None = None
    fallback_reason: str | None = None
    if failures:
        fallback_from = str(failures[0][0])
        fallback_reason = _reason(failures[0][1])
        route_arm = plan.worker
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
    try:
        adapter = registry.get(plan.adapter)
    except KeyError as error:
        raise SubmitRefused(str(error)) from error
    _preflight(plan, adapter.name)

    root = ledger_root
    root.mkdir(parents=True, exist_ok=True)
    key = fingerprint(project_root, role, title, value)
    # An explicit owner makes a bridge task. It has no submitting session, so no
    # Claude Code hook can match it.
    owner_kind = "bridge" if owner else "session"
    owner_id = owner or os.environ.get("CLAUDE_CODE_SESSION_ID") or None
    submitter_session_id = None if owner else owner_id
    duplicate = _find_duplicate(
        root, key, owner_kind, owner_id, fast=plan.fast, route_arm=route_arm
    )
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
        "task_root": str(root),
        "config_path": str(config_path) if config_path is not None else None,
        "config_sha256": config_sha256,
        "role": role,
        "title": title,
        "worker": plan.worker,
        "route_arm": route_arm,
        "route_policy": route_policy,
        "route_fallback_from": fallback_from,
        "route_fallback_reason": fallback_reason,
        "unhealthy_override": override,
        "health_probe": health_probe,
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
        "submitter_session_id": submitter_session_id,
        "owner_kind": owner_kind,
        "owner_id": owner_id,
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
    register_completion_watch(
        project_root, task_id, submitter_session_id, explicit_root(project_root, root)
    )

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
