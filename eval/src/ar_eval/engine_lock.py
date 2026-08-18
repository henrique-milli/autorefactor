"""Serialize access to the shared Compose engine across loop steps."""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import TracebackType
from typing import Any


class EngineLock:
    """
    Advisory file lock so stimulus / rebuild / retest do not race the same engine.

    Uses ``fcntl.flock`` on Linux. ``timeout`` is seconds to wait for the lock.
    """

    def __init__(
        self,
        path: Path,
        *,
        timeout: float = 1800.0,
        poll: float = 0.5,
    ) -> None:
        self.path = path
        self.timeout = timeout
        self.poll = poll
        self._fh: Any = None

    def __enter__(self) -> EngineLock:
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+", encoding="utf-8")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fh.seek(0)
                self._fh.truncate()
                self._fh.write(f"pid={os.getpid()} at={time.time()}\n")
                self._fh.flush()
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"engine lock not acquired within {self.timeout}s: {self.path}"
                    )
                time.sleep(self.poll)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        import fcntl

        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None
