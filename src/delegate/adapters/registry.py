"""Which adapter implementations exist."""

from __future__ import annotations

from delegate.adapters.base import WorkerAdapter
from delegate.adapters.claude import ClaudeAdapter
from delegate.adapters.codex import CodexAdapter
from delegate.adapters.opencode import OpenCodeAdapter

_ADAPTERS: dict[str, WorkerAdapter] = {
    "claude": ClaudeAdapter(),
    "codex": CodexAdapter(),
    "opencode": OpenCodeAdapter(),
}


def get(name: str) -> WorkerAdapter:
    adapter = _ADAPTERS.get(name)
    if adapter is None:
        known = ", ".join(sorted(_ADAPTERS))
        raise KeyError(f"unknown adapter: {name} (available: {known})")
    return adapter
