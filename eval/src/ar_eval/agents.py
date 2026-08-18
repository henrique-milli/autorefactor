"""Cursor SDK local agents for critic / maintainer roles."""

from __future__ import annotations

import concurrent.futures
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

AgentHeartbeat = Callable[[str], None]


class CursorSdkMissing(RuntimeError):
    """Raised when the optional cursor-sdk extra is not installed."""


def _require_sdk():
    try:
        from cursor_sdk import (
            Agent,
            AgentOptions,
            CursorAgentError,
            LocalAgentOptions,
            StdioMcpServerConfig,
        )
    except ImportError as exc:
        raise CursorSdkMissing(
            "cursor-sdk is required. Install with: uv sync --extra eval"
        ) from exc
    return Agent, AgentOptions, CursorAgentError, LocalAgentOptions, StdioMcpServerConfig


def mcp_autorefactor_stdio(
    *,
    remote_url: str = "http://127.0.0.1:8765",
    command: str | None = None,
    workspace: Path | None = None,
) -> dict[str, Any]:
    """Inline stdio MCP server config for Cursor SDK (HTTP remote via autorefactor-mcp)."""
    *_, StdioMcpServerConfig = _require_sdk()
    env = {
        "AUTOREFACTOR_REMOTE_URL": remote_url,
        "AUTOREFACTOR_MCP_TRANSPORT": "stdio",
    }
    if command is not None:
        cmd, args = command, []
    else:
        cmd, args = "uv", ["run", "autorefactor-mcp"]
    kwargs: dict[str, Any] = {"command": cmd, "args": args, "env": env}
    if workspace is not None:
        ws = str(workspace.resolve())
        env["AUTOREFACTOR_MCP_WORKSPACE"] = ws
        kwargs["cwd"] = ws
    return {
        "autorefactor": StdioMcpServerConfig(**kwargs),
    }


def _try_cancel_run(run: Any) -> None:
    for name in ("cancel", "stop", "abort"):
        fn = getattr(run, name, None)
        if not callable(fn):
            continue
        try:
            fn()
        except Exception:
            pass
        return


def _wait_run(run: Any, timeout_s: float | None) -> tuple[Any | None, bool]:
    """Wait for ``run.wait()``. Returns ``(result, timed_out)``."""
    if timeout_s is None or timeout_s <= 0:
        return run.wait(), False
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(run.wait)
        try:
            return future.result(timeout=timeout_s), False
        except concurrent.futures.TimeoutError:
            _try_cancel_run(run)
            return None, True
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def run_local_agent(
    *,
    prompt: str,
    cwd: Path,
    api_key: str | None = None,
    model: str = "composer-2.5",
    mcp_servers: dict[str, Any] | None = None,
    on_heartbeat: AgentHeartbeat | None = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """
    One-shot local Cursor agent. Returns status metadata (never raises on run failure).

    Distinguishes startup failure (raises) from run failure (status=error in return).

    Note: ``mcp_servers`` must live on ``AgentOptions`` (positional ``options``), not as
    a top-level ``Agent.create`` keyword — the installed SDK rejects that kwarg.
    """
    Agent, AgentOptions, CursorAgentError, LocalAgentOptions, _ = _require_sdk()
    from ar_eval.secrets import resolve_cursor_api_key

    key = resolve_cursor_api_key(explicit=api_key)
    if not key:
        raise RuntimeError(
            "CURSOR_API_KEY is required. Put it in eval/secrets.local.env "
            "(or repo .env), or export CURSOR_API_KEY."
        )

    options = AgentOptions(
        api_key=key,
        model=model,
        local=LocalAgentOptions(cwd=str(cwd.resolve())),
        mcp_servers=mcp_servers,
    )

    try:
        with Agent.create(options) as agent:
            agent_id = getattr(agent, "agent_id", None) or getattr(agent, "agentId", None)
            # Pass MCP again on send — SDK does not persist inline servers across resumes.
            send_opts = {"mcp_servers": mcp_servers} if mcp_servers else None
            run = agent.send(prompt, send_opts) if send_opts else agent.send(prompt)
            run_id = getattr(run, "id", None)
            started = time.monotonic()
            stop = threading.Event()

            def _beat() -> None:
                while not stop.wait(15.0):
                    elapsed = int(time.monotonic() - started)
                    bits = [f"waiting {elapsed}s"]
                    if agent_id:
                        bits.append(f"agent={agent_id}")
                    if run_id:
                        bits.append(f"run={run_id}")
                    if on_heartbeat is not None:
                        on_heartbeat(" · ".join(bits))

            if on_heartbeat is not None:
                ids = []
                if agent_id:
                    ids.append(f"agent={agent_id}")
                if run_id:
                    ids.append(f"run={run_id}")
                on_heartbeat("started" + (f" · {' · '.join(ids)}" if ids else ""))
                ticker = threading.Thread(target=_beat, name="ar-eval-agent-hb", daemon=True)
                ticker.start()
            else:
                ticker = None
            t0 = time.perf_counter()
            try:
                result, timed_out = _wait_run(run, timeout_s)
                duration_s = time.perf_counter() - t0
                if timed_out:
                    return {
                        "ok": False,
                        "status": "timeout",
                        "timed_out": True,
                        "agent_id": agent_id,
                        "run_id": run_id,
                        "result_text": None,
                        "duration_s": duration_s,
                    }
            finally:
                stop.set()
                if ticker is not None:
                    ticker.join(timeout=1.0)
            assert result is not None
            status = getattr(result, "status", None)
            if hasattr(status, "value"):
                status = status.value
            status_s = str(status) if status is not None else "unknown"
            text = None
            if hasattr(result, "result"):
                text = result.result
            ok = status_s.lower() in {"finished", "success", "completed", "ok"}
            return {
                "ok": ok,
                "status": status_s,
                "timed_out": False,
                "agent_id": agent_id,
                "run_id": run_id,
                "result_text": text if isinstance(text, str) else None,
                "duration_s": duration_s,
            }
    except CursorAgentError as exc:
        retryable = getattr(exc, "is_retryable", None)
        raise RuntimeError(
            f"Cursor agent startup failed: {exc}"
            + (f" retryable={retryable}" if retryable is not None else "")
        ) from exc
    except TypeError as exc:
        # Surface SDK option-shape mistakes clearly (e.g. wrong create kwargs).
        raise RuntimeError(f"Cursor agent options invalid: {exc}") from exc


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
