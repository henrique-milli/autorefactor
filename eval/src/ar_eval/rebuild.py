"""Rebuild / restart the shared Compose engine after alpha patches."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ar_eval.engine import wait_healthy
from ar_eval.timing import record_duration

RebuildProgress = Callable[[str], None]


def compose_up_cmd(*, build: bool = True, service: str = "autorefactor") -> list[str]:
    """``docker compose up -d`` for the engine service, optional ``--build``."""
    cmd = ["docker", "compose", "up", "-d"]
    if build:
        cmd.append("--build")
    if service:
        cmd.append(service)
    return cmd


def compose_rebuild_cmd() -> list[str]:
    """Restart the engine so bind-mounted ``src/`` is re-imported (no image rebuild)."""
    return ["docker", "compose", "restart", "autorefactor"]


def ensure_engine(
    compose_dir: Path,
    *,
    engine_url: str | None = None,
    health_timeout: float = 180.0,
    build: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Ensure Compose services are up (and optionally rebuilt), then wait for health.

    Used at loop/wave start so you do not need a manual ``docker compose up`` first.
    """
    cmd = compose_up_cmd(build=build)
    timing: dict[str, Any] = {}
    if dry_run:
        return {
            "ok": True,
            "cmd": cmd,
            "dry_run": True,
            "cwd": str(compose_dir),
            "timing": timing,
        }
    with record_duration("compose_up_s", timing):
        subprocess.run(cmd, cwd=str(compose_dir), check=True)
    with record_duration("engine_health_wait_s", timing):
        health = wait_healthy(engine_url, timeout=health_timeout)
    return {
        "ok": True,
        "cmd": cmd,
        "cwd": str(compose_dir),
        "health": health,
        "timing": timing,
    }


def rebuild_engine(
    compose_dir: Path,
    *,
    engine_url: str | None = None,
    health_timeout: float = 180.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    ``docker compose restart autorefactor`` then wait until ``/health`` succeeds.

    Host ``./src`` is bind-mounted at ``/app/src``; a process restart picks up alpha
    patches without ``pip install`` / image rebuild. Use ``ensure_engine(build=True)``
    when ``pyproject.toml`` or the Dockerfile itself changed.

    ``dry_run`` returns the command without executing (for unit tests).
    Restarts only the ``autorefactor`` service (not web).
    """
    cmd = compose_rebuild_cmd()
    timing: dict[str, Any] = {}
    if dry_run:
        return {
            "ok": True,
            "cmd": cmd,
            "dry_run": True,
            "cwd": str(compose_dir),
            "timing": timing,
        }
    with record_duration("compose_restart_s", timing):
        subprocess.run(cmd, cwd=str(compose_dir), check=True)
    with record_duration("engine_health_wait_s", timing):
        health = wait_healthy(engine_url, timeout=health_timeout)
    return {
        "ok": True,
        "cmd": cmd,
        "cwd": str(compose_dir),
        "health": health,
        "timing": timing,
    }
