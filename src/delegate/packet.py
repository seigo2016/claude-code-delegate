"""The task description handed to a worker.

A packet carries only what this task adds. Role instructions, model choice and
the prohibitions come from configuration, so the caller cannot negotiate them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

REQUIRED_FIELDS = ("objective", "read", "allowed_writes", "required_evidence", "host_only")
STRING_LISTS = ("read", "read_optional", "allowed_writes", "required_evidence", "forbidden")
GLOB_CHARACTERS = frozenset("*?[")


def validate(value: Any) -> dict[str, Any]:
    """Return the packet, or raise ``ValueError`` naming what is wrong with it."""
    if not isinstance(value, dict):
        raise ValueError("task packet must be a JSON object")

    missing = [field for field in REQUIRED_FIELDS if field not in value]
    if missing:
        raise ValueError(f"task packet missing fields: {', '.join(missing)}")
    if not isinstance(value["objective"], str) or not value["objective"].strip():
        raise ValueError("objective must be a non-empty string")
    for field in STRING_LISTS:
        if field not in value:
            continue
        item = value[field]
        if not isinstance(item, list) or not all(isinstance(entry, str) for entry in item):
            raise ValueError(f"{field} must be an array of strings")
    if not isinstance(value["host_only"], bool):
        raise ValueError("host_only must be a boolean")
    if "escalation_reason" in value and (
        not isinstance(value["escalation_reason"], str) or not value["escalation_reason"].strip()
    ):
        raise ValueError("escalation_reason must be a non-empty string")
    if "user_directed" in value and not isinstance(value["user_directed"], bool):
        raise ValueError("user_directed must be a boolean")
    return value


def missing_reads(value: dict[str, Any], project_root: Path) -> list[str]:
    """The ``read`` entries that name a concrete path and do not exist.

    Relative entries are taken from the project root. Globs and URLs cannot be
    checked this way, and ``read_optional`` is allowed to be absent, so none of
    those are reported.
    """
    missing = []
    for entry in value["read"]:
        if "://" in entry or GLOB_CHARACTERS & set(entry):
            continue
        try:
            path = Path(entry).expanduser()
        except RuntimeError:
            # ``~nosuchuser/x`` has no home to resolve against, so nothing is there to read.
            missing.append(entry)
            continue
        if not (path if path.is_absolute() else project_root / path).exists():
            missing.append(entry)
    return missing
