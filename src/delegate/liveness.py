"""Whether a detached worker is still running.

The worker holds a lock for its whole life, so taking that lock means it is gone.
No pid is consulted, so a recycled pid cannot pass for a live worker.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from collections.abc import Iterator
from pathlib import Path


class AlreadyHeld(Exception):
    """Another process is already supervising this task."""


@contextlib.contextmanager
def hold(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise AlreadyHeld(f"another process holds {path}") from error
        os.write(fd, f"{os.getpid()}\n".encode())
        yield
    finally:
        os.close(fd)


def worker_alive(path: Path) -> bool:
    """True while some other process holds the worker lock."""
    try:
        fd = os.open(path, os.O_WRONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)
