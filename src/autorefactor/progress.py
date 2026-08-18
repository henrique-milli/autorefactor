"""Lightweight analyze progress reporting for jobs / MCP pollers."""

from __future__ import annotations

from typing import Callable, Protocol


class ProgressReporter(Protocol):
    def __call__(
        self,
        phase: str,
        percent: float | None = None,
        detail: str | None = None,
    ) -> None: ...


ProgressCallback = Callable[[str, float | None, str | None], None]


def noop_progress(
    phase: str, percent: float | None = None, detail: str | None = None
) -> None:
    return None
