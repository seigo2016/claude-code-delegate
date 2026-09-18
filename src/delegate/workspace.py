"""What changed in the repository while a worker ran.

Worker events can attribute explicit file edits. Git can only show that the
shared work tree changed, not which concurrent process changed it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# The task's own bookkeeping is not the worker's doing.
IGNORED_PREFIXES = (
    ".claude/logs/",
    ".tmp/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".mypy_cache/",
)
IGNORED_SUBSTRINGS = (
    "/__pycache__/",
    "__pycache__/",
    "/.pytest_cache/",
    "/.ruff_cache/",
    "/.mypy_cache/",
)
IGNORED_EXTENSIONS = (
    ".pyc",
    ".pyo",
)


def _is_ignored(path: str) -> bool:
    if path.startswith(IGNORED_PREFIXES):
        return True
    if any(sub in path for sub in IGNORED_SUBSTRINGS):
        return True
    if path.endswith(IGNORED_EXTENSIONS):
        return True
    return False


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
        if len(entry) > 3 and not _is_ignored(entry[3:])
    }


def unauthorized(before: set[str], after: set[str], allowed: list[str]) -> list[str]:
    """Paths written that no allowed entry covers."""
    return sorted(
        path
        for path in after - before
        if not any(path == entry or path.startswith(entry.rstrip("/") + "/") for entry in allowed)
    )


def relative_paths(project_root: Path, paths: tuple[str, ...]) -> set[str]:
    """Worker-reported paths that are inside the work tree, made relative to it."""
    root = Path(os.path.abspath(project_root))
    relative = set()
    for raw in paths:
        path = Path(raw)
        candidate = path if path.is_absolute() else root / path
        try:
            relative.add(Path(os.path.abspath(candidate)).relative_to(root).as_posix())
        except ValueError:
            continue
    return relative
