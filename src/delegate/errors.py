"""The exit code and the default reason of every error `delegate` reports.

An error prints as ``{"error": <name>, "reason": <sentence>}``. 0 is success, 2
is ``WAKE`` (a task is ready) and is never an error.
"""

from __future__ import annotations

#: name -> (exit code, reason). A caller may give a more specific reason.
ERRORS: dict[str, tuple[int, str]] = {
    "no_settings": (1, "the settings file cannot be read"),
    "invalid_config": (1, "the settings are not valid"),
    "invalid_packet": (1, "the packet cannot be used"),
    "submit_refused": (1, "the request will not start a worker"),
    "task_not_found": (1, "no such task in this task root"),
    "already_delivered": (3, "the result was already collected"),
    "task_not_terminal": (4, "the task has not finished"),
    "owner_mismatch": (5, "the task belongs to another owner"),
    "nested_submit": (6, "a delegated worker must not delegate; report back to the Chair instead"),
    "worker_unhealthy": (7, "the worker's last tasks all failed for the worker's own reasons"),
}


def exit_code(name: str) -> int:
    return ERRORS[name][0]
