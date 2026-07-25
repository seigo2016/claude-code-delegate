"""The task description handed to a worker.

A packet carries only what this task adds. Role instructions, model choice and
the prohibitions come from configuration, so the caller cannot negotiate them.
"""

from __future__ import annotations

from typing import Any

REQUIRED_FIELDS = ("objective", "read", "allowed_writes", "required_evidence", "host_only")
STRING_LISTS = ("read", "allowed_writes", "required_evidence", "forbidden")


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
    return value
