"""Recover an unresponsive remote autorefactor engine without user intervention."""

from __future__ import annotations

import os
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any


def _remote_base_url() -> str | None:
    raw = (os.environ.get("AUTOREFACTOR_REMOTE_URL") or "").strip().rstrip("/")
    return raw or None


def _api_base(base: str) -> str:
    if base.endswith("/mcp"):
        return base[: -len("/mcp")] or base
    return base


def _health_url() -> str | None:
    base = _remote_base_url()
    if not base:
        return None
    return f"{_api_base(base)}/health"


def remote_engine_healthy(*, timeout: float = 3.0) -> bool:
    url = _health_url()
    if not url:
        return False
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def restart_remote_engine(*, wait_s: float = 4.0) -> dict[str, Any]:
    """Restart the Docker engine container and wait for /health."""
    container = (os.environ.get("AUTOREFACTOR_DOCKER_CONTAINER") or "autorefactor").strip()
    proc = subprocess.run(
        ["docker", "restart", container],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    out = {
        "ok": proc.returncode == 0,
        "container": container,
        "stdout": (proc.stdout or "").strip()[-500:],
        "stderr": (proc.stderr or "").strip()[-500:],
    }
    if wait_s > 0:
        time.sleep(wait_s)
    out["healthy_after"] = remote_engine_healthy(timeout=10.0)
    return out


def ensure_remote_engine(*, allow_restart: bool = True) -> bool:
    """Return True when remote /health responds; optionally restart Docker once."""
    if remote_engine_healthy():
        return True
    if not allow_restart:
        return False
    restart_remote_engine()
    return remote_engine_healthy(timeout=10.0)
