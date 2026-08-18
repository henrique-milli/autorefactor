"""Tiny perf-counter timing helpers for eval instrumentation."""

from __future__ import annotations

import time
from collections.abc import MutableMapping
from contextlib import contextmanager
from typing import Any, Iterator


@contextmanager
def record_duration(
    label: str,
    out: MutableMapping[str, Any],
) -> Iterator[None]:
    """Record seconds under ``out[label]``.

    Overwrites if called multiple times with the same label.
    """

    start = time.perf_counter()
    try:
        yield
    finally:
        out[label] = time.perf_counter() - start

