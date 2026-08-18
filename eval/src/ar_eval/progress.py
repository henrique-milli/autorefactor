"""Live / line progress reporter for ``ar-eval loop``."""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, TextIO

from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

ProgressCallback = Callable[[str], None]


def format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def format_analyze_job(job: dict[str, Any]) -> str:
    """Compact ``phase 75% · detail`` line from an analyze job payload."""
    phase = str(job.get("phase") or job.get("status") or job.get("state") or "analyze")
    percent = job.get("percent")
    detail = job.get("detail")
    parts = [phase]
    if isinstance(percent, (int, float)):
        parts.append(f"{int(percent)}%")
    if detail:
        parts.append(str(detail))
    return " · ".join(parts)


def _persona_short(name: str) -> str:
    stem = name.rsplit("/", 1)[-1]
    if stem.endswith(".md"):
        stem = stem[:-3]
    return stem.removeprefix("user_")


class LoopProgress:
    """
    Rich Live dashboard on a TTY, timestamped one-liners otherwise.

    Heartbeat ticks elapsed time during blocking waits. Safe to call from
    analyze poll callbacks and a daemon thread around ``run.wait()``.
    """

    def __init__(
        self,
        *,
        file: TextIO | None = None,
        force_lines: bool = False,
        refresh_per_second: float = 1.0,
    ) -> None:
        self._file = file or sys.stderr
        self._force_lines = force_lines
        self._console = Console(file=self._file, highlight=False, soft_wrap=True)
        self.iterations = 0
        self.iteration = 0
        self.episode = ""
        self.persona = ""
        self.phase_name = "idle"
        self.detail = ""
        self.last_note = ""
        self.rows: list[dict[str, Any]] = []
        self._started = time.monotonic()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ticker: threading.Thread | None = None
        self._started_live = False
        use_live = (not force_lines) and self._file.isatty()
        self._live: Live | None = None
        if use_live:
            self._live = Live(
                self._render(),
                console=self._console,
                refresh_per_second=refresh_per_second,
                transient=False,
            )

    def start(self, iterations: int) -> None:
        self.iterations = iterations
        self._started = time.monotonic()
        if self._live is not None and not self._started_live:
            self._live.start()
            self._started_live = True
        self._ticker = threading.Thread(target=self._tick_loop, name="ar-eval-progress", daemon=True)
        self._ticker.start()
        self.phase("start", f"{iterations} iteration(s)")

    def _tick_loop(self) -> None:
        while not self._stop.wait(1.0):
            self._refresh()

    def set_context(
        self,
        *,
        iteration: int | None = None,
        episode: str | None = None,
        persona: str | None = None,
    ) -> None:
        with self._lock:
            if iteration is not None:
                self.iteration = iteration
            if episode is not None:
                self.episode = episode
            if persona is not None:
                self.persona = persona
        self._refresh()

    def phase(self, name: str, detail: str = "") -> None:
        with self._lock:
            self.phase_name = name
            if detail:
                self.detail = detail
        if self._live is None:
            self._emit_line(name, detail)
        else:
            self._refresh()

    def heartbeat(self, detail: str = "") -> None:
        with self._lock:
            if detail:
                self.detail = detail
        if self._live is None:
            if detail:
                self._emit_line(self.phase_name, detail)
        else:
            self._refresh()

    def note(self, text: str) -> None:
        with self._lock:
            self.last_note = text
        if self._live is None:
            self._emit_line("note", text)
        else:
            self._refresh()

    def finish_iteration(self, row: dict[str, Any]) -> None:
        with self._lock:
            self.rows.append(row)
            self.last_note = self._row_note(row)
        if self._live is None:
            self._emit_line("iter-done", self._row_note(row))
        else:
            self._refresh()

    def stop(self) -> None:
        self._stop.set()
        if self._ticker is not None:
            self._ticker.join(timeout=2.0)
            self._ticker = None
        if self._live is not None and self._started_live:
            self._refresh()
            self._live.stop()
            self._started_live = False

    def elapsed(self) -> float:
        return time.monotonic() - self._started

    def _refresh(self) -> None:
        if self._live is None or not self._started_live:
            return
        with self._lock:
            self._live.update(self._render())

    def _emit_line(self, name: str, detail: str = "") -> None:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        elapsed = format_elapsed(self.elapsed())
        iter_s = f"{self.iteration}/{self.iterations}" if self.iterations else "-"
        bits = [f"[{ts}]", f"+{elapsed}", f"iter {iter_s}", name]
        if self.episode:
            bits.append(self.episode)
        if detail:
            bits.append(detail)
        self._console.print(" ".join(bits))

    def _render(self) -> Group:
        header = Text()
        header.append("ar-eval loop  ", style="bold")
        if self.iterations:
            header.append(f"iter {self.iteration or 0}/{self.iterations}  ")
        header.append(f"elapsed {format_elapsed(self.elapsed())}", style="dim")

        meta = Text()
        if self.episode:
            meta.append(f"episode {self.episode}  ")
        if self.persona:
            meta.append(f"persona {_persona_short(self.persona)}")

        phase = Text()
        phase.append("phase   ", style="dim")
        phase.append(self.phase_name, style="bold cyan")
        if self.detail:
            phase.append(f" · {self.detail}")

        last = Text()
        last.append("last    ", style="dim")
        last.append(self.last_note or "—")

        table = Table(box=None, show_header=True, pad_edge=False, expand=False)
        table.add_column("#", style="dim", width=3)
        table.add_column("user")
        table.add_column("smoke")
        table.add_column("retest")
        for row in self.rows:
            table.add_row(
                str(row.get("iteration") or ""),
                str(row.get("user") or "—"),
                str(row.get("smoke") or "—"),
                str(row.get("retest") or "—"),
            )
        if self.iteration and (
            not self.rows or self.rows[-1].get("iteration") != self.iteration
        ):
            table.add_row(str(self.iteration), "…", "—", "—")

        parts: list[Any] = [header, meta, phase, last]
        if self.rows or self.iteration:
            parts.append(table)
        return Group(*parts)

    @staticmethod
    def _row_note(row: dict[str, Any]) -> str:
        parts = [f"iter {row.get('iteration')}"]
        if row.get("user"):
            parts.append(f"user {row['user']}")
        if row.get("smoke"):
            parts.append(f"smoke {row['smoke']}")
        if row.get("retest"):
            parts.append(f"retest {row['retest']}")
        if row.get("sticky"):
            parts.append(f"sticky {row['sticky']}")
        return " · ".join(parts)


def noop_progress() -> LoopProgress:
    """Line-mode reporter writing nowhere — for tests that only need the API."""
    from io import StringIO

    return LoopProgress(file=StringIO(), force_lines=True)


def format_retest_cell(payload: dict[str, Any]) -> str:
    """Dashboard cell: ``2/2 pass`` / ``1/2 fail``, never a lone probe score."""
    retests = payload.get("retests") or []
    if isinstance(retests, list) and retests:
        n = len(retests)
        ok_n = sum(1 for r in retests if isinstance(r, dict) and r.get("passed"))
        return f"{ok_n}/{n} {'pass' if ok_n == n else 'fail'}"
    retest = payload.get("retest") or {}
    if not retest:
        return "—"
    if retest.get("skipped"):
        return "—"
    passed = retest.get("passed")
    if "episodes_passed" in retest and "episodes_total" in retest:
        ok_n = int(retest.get("episodes_passed") or 0)
        n = int(retest.get("episodes_total") or 0)
        if n:
            return f"{ok_n}/{n} {'pass' if passed else 'fail'}"
    return "pass" if passed else "fail"


def format_loop_summary(summary: dict[str, Any], *, artifacts: str | None = None) -> str:
    """Short human end-of-run table for stdout."""
    lines = ["ar-eval loop done"]
    results = summary.get("results") or []
    lines.append(f"iterations {len(results)}/{summary.get('iterations') or len(results)}")
    for it in results:
        n = it.get("iteration")
        persona_list = it.get("personas") or ([it.get("persona")] if it.get("persona") else [])
        persona = (
            _persona_short(str(persona_list[0]))
            if len(persona_list) == 1
            else f"{len(persona_list)}p"
        )
        gates = [
            bool((s.get("feedback_gate") or {}).get("product_use_ok"))
            for s in (it.get("stimulus") or [])
            if not s.get("skipped_agents")
        ]
        user = "ok" if gates and any(gates) else ("skip" if it.get("skipped_agents") else "fail")
        smoke = it.get("smoke") or {}
        smoke_s = "—" if not smoke else ("pass" if smoke.get("ok") else "fail")
        retest_s = format_retest_cell(it)
        lines.append(f"  {n}  {persona or '-'}  user={user}  smoke={smoke_s}  retest={retest_s}")
    if artifacts:
        lines.append(f"artifacts {artifacts}")
    return "\n".join(lines) + "\n"
