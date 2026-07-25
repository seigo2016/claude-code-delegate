"""Whether a detached worker is still running, decided by an exclusive lock.

The worker holds ``worker.lock`` for its whole life. Anyone else can ask the
question by trying to take the same lock: success means nobody holds it, so the
worker is gone. This never consults a pid, so a recycled pid cannot be mistaken
for a live worker, and it needs no ``/proc``.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from collections.abc import Iterator
from pathlib import Path


@contextlib.contextmanager
def hold(path: Path) -> Iterator[None]:
    """Hold the worker lock for the duration of the block."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
