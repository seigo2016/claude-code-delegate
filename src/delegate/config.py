"""What a role needs, and which backends can supply it.

Roles are written against capabilities, not model names, so the same set of roles
works whichever backend a repository has configured. Resolution happens before a
worker is started, so a missing model is an error you read, not a failure that
surfaces minutes later inside someone else's CLI.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT = 1800


class ConfigError(Exception):
    """Configuration that cannot be acted on, described in the message."""


@dataclass(frozen=True)
class Role:
    name: str
    capability: str
    effort: str
    task_class: str
    timeout: int = DEFAULT_TIMEOUT
    requires_allowed_writes: bool = False


@dataclass(frozen=True)
class Worker:
    name: str
    adapter: str
    enabled: bool
    models: dict[str, str]


@dataclass(frozen=True)
class Plan:
    """Everything needed to start one task, decided up front."""

    role: Role
    worker: str
    adapter: str
    model: str
    effort: str
    timeout: int


@dataclass(frozen=True)
class Settings:
    roles: dict[str, Role]
    workers: dict[str, Worker]
    default_worker: str | None

    def plan(self, role_name: str, *, worker: str | None = None, effort: str | None = None) -> Plan:
        role = self.roles.get(role_name)
        if role is None:
            known = ", ".join(sorted(self.roles)) or "none configured"
            raise ConfigError(f"unknown role: {role_name} (configured roles: {known})")

        chosen = self._choose_worker(worker)
        model = chosen.models.get(role.capability)
        if not model:
            raise ConfigError(
                f"{chosen.name} has no model for capability {role.capability}; "
                f"add it under [workers.{chosen.name}].models"
            )
        return Plan(
            role=role,
            worker=chosen.name,
            adapter=chosen.adapter,
            model=model,
            effort=effort or role.effort,
            timeout=role.timeout,
        )

    def _choose_worker(self, requested: str | None) -> Worker:
        if requested is not None:
            worker = self.workers.get(requested)
            if worker is None:
                known = ", ".join(sorted(self.workers)) or "none configured"
                raise ConfigError(f"unknown worker: {requested} (configured workers: {known})")
            if not worker.enabled:
                raise ConfigError(f"worker {requested} is declared but not enabled")
            return worker

        enabled = [worker for worker in self.workers.values() if worker.enabled]
        if not enabled:
            raise ConfigError(
                "no worker is enabled; declare one under [workers.<name>] with enabled = true"
            )
        if self.default_worker is not None:
            for worker in enabled:
                if worker.name == self.default_worker:
                    return worker
            raise ConfigError(f"default_worker {self.default_worker} is not an enabled worker")
        if len(enabled) > 1:
            raise ConfigError("several workers are enabled; set default_worker or pass --worker")
        return enabled[0]


def load(path: Path) -> Settings:
    with path.open("rb") as handle:
        raw: dict[str, Any] = tomllib.load(handle)

    roles = {
        name: Role(
            name=name,
            capability=str(body["capability"]),
            effort=str(body["effort"]),
            task_class=str(body["task_class"]),
            timeout=int(body.get("timeout", DEFAULT_TIMEOUT)),
            requires_allowed_writes=bool(body.get("requires_allowed_writes", False)),
        )
        for name, body in raw.get("roles", {}).items()
    }
    workers = {
        name: Worker(
            name=name,
            adapter=str(body["adapter"]),
            enabled=bool(body.get("enabled", False)),
            models={str(k): str(v) for k, v in body.get("models", {}).items()},
        )
        for name, body in raw.get("workers", {}).items()
    }
    default_worker = raw.get("default_worker")
    return Settings(
        roles=roles,
        workers=workers,
        default_worker=str(default_worker) if default_worker is not None else None,
    )
