"""What changed in the repository while a worker ran.

A declared write scope is only a request until someone compares it with what
happened. Git is what we have to compare with; without it we say so rather than
implying a check took place.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# The task's own bookkeeping is not the worker's doing.
IGNORED = (".claude/logs/",)


def changed_paths(project_root: Path) -> set[str] | None:
    """Paths git reports as changed, or None if this is not a git work tree."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return {
        entry[3:]
        for entry in result.stdout.split("\0")
        if len(entry) > 3 and not entry[3:].startswith(IGNORED)
    }


def unauthorized(before: set[str], after: set[str], allowed: list[str]) -> list[str]:
    """Paths written that no allowed entry covers."""
    return sorted(
        path
        for path in after - before
        if not any(path == entry or path.startswith(entry.rstrip("/") + "/") for entry in allowed)
    )
