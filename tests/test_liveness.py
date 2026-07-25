"""Worker liveness must not depend on /proc and must survive PID reuse."""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from delegate import liveness

HOLDER = """
import sys, time
sys.path.insert(0, {src!r})
from delegate import liveness
with liveness.hold(__import__("pathlib").Path({lock!r})):
    __import__("pathlib").Path({ready!r}).write_text("up")
    while not __import__("pathlib").Path({stop!r}).exists():
        time.sleep(0.01)
"""


@contextmanager
def holding(tmp_path: Path) -> Iterator[Path]:
    """Run another process that holds the lock until the block ends."""
    lock, ready, stop = tmp_path / "worker.lock", tmp_path / "ready", tmp_path / "stop"
    source = str(Path(__file__).resolve().parents[1] / "src")
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            HOLDER.format(src=source, lock=str(lock), ready=str(ready), stop=str(stop)),
        ]
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "holder process never started"
        yield lock
    finally:
        stop.write_text("")
        holder.wait(timeout=10)


def test_unheld_lock_reports_the_worker_as_gone(tmp_path: Path) -> None:
    assert liveness.worker_alive(tmp_path / "never-created.lock") is False

    (tmp_path / "stale.lock").write_text("")
    assert liveness.worker_alive(tmp_path / "stale.lock") is False


def test_worker_is_alive_only_while_it_holds_the_lock(tmp_path: Path) -> None:
    with holding(tmp_path) as lock:
        assert liveness.worker_alive(lock) is True

    assert liveness.worker_alive(lock) is False


def test_a_second_holder_is_told_so_rather_than_failing_obscurely(tmp_path: Path) -> None:
    with holding(tmp_path) as lock, pytest.raises(liveness.AlreadyHeld), liveness.hold(lock):
        pass
