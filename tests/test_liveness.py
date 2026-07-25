"""Worker liveness must not depend on /proc and must survive PID reuse.

The broker decides whether a detached worker is still running by trying to take
the worker's exclusive lock. Only a live worker can hold it, so a PID that has
been recycled by an unrelated process can never be mistaken for the worker.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

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


def test_unheld_lock_reports_the_worker_as_gone(tmp_path: Path) -> None:
    assert liveness.worker_alive(tmp_path / "never-created.lock") is False

    (tmp_path / "stale.lock").write_text("")
    assert liveness.worker_alive(tmp_path / "stale.lock") is False


def test_worker_is_alive_only_while_it_holds_the_lock(tmp_path: Path) -> None:
    src = str(Path(__file__).resolve().parents[1] / "src")
    lock = tmp_path / "worker.lock"
    ready = tmp_path / "ready"
    stop = tmp_path / "stop"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            HOLDER.format(src=src, lock=str(lock), ready=str(ready), stop=str(stop)),
        ]
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "holder process never started"

        assert liveness.worker_alive(lock) is True
    finally:
        stop.write_text("")
        holder.wait(timeout=10)

    assert liveness.worker_alive(lock) is False
