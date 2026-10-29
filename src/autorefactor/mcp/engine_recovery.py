"""Detect an unresponsive remote autorefactor engine.

There is no local process manager (the engine runs as a plain local server),
so recovery is health-check only: report whether ``/health`` responds.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request


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


def ensure_remote_engine(*, timeout: float = 10.0) -> bool:
    """Return True when remote ``/health`` responds."""
    return remote_engine_healthy(timeout=timeout)
