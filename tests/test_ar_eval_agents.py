"""Unit tests for Cursor SDK agent wiring (no live agent)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ar_eval.agents import mcp_autorefactor_stdio, run_local_agent


def test_mcp_autorefactor_stdio_shape() -> None:
    servers = mcp_autorefactor_stdio(remote_url="http://127.0.0.1:8765")
    assert "autorefactor" in servers
    cfg = servers["autorefactor"]
    assert cfg.command == "uv"
    assert "autorefactor-mcp" in cfg.args
    assert cfg.env["AUTOREFACTOR_REMOTE_URL"] == "http://127.0.0.1:8765"
    assert "AUTOREFACTOR_MCP_WORKSPACE" not in cfg.env
    assert getattr(cfg, "cwd", None) in (None, "")


def test_mcp_autorefactor_stdio_pins_workspace(tmp_path: Path) -> None:
    servers = mcp_autorefactor_stdio(
        remote_url="http://127.0.0.1:8765", workspace=tmp_path
    )
    cfg = servers["autorefactor"]
    ws = str(tmp_path.resolve())
    assert cfg.cwd == ws
    assert cfg.env["AUTOREFACTOR_MCP_WORKSPACE"] == ws
    assert cfg.env["AUTOREFACTOR_REMOTE_URL"] == "http://127.0.0.1:8765"


def test_run_local_agent_passes_mcp_via_agent_options(tmp_path: Path) -> None:
    pytest.importorskip("cursor_sdk")
    from cursor_sdk import AgentOptions, StdioMcpServerConfig

    fake_run = MagicMock()
    fake_run.id = "run-1"
    fake_result = MagicMock()
    fake_result.status = "finished"
    fake_result.result = "done"
    fake_run.wait.return_value = fake_result

    fake_agent = MagicMock()
    fake_agent.agent_id = "agent-1"
    fake_agent.send.return_value = fake_run
    fake_agent.__enter__.return_value = fake_agent
    fake_agent.__exit__.return_value = None

    mcp = {
        "autorefactor": StdioMcpServerConfig(
            command="uv",
            args=["run", "autorefactor-mcp"],
            env={"AUTOREFACTOR_REMOTE_URL": "http://127.0.0.1:8765"},
        )
    }

    with (
        patch("cursor_sdk.Agent.create", return_value=fake_agent) as create,
        patch("ar_eval.secrets.resolve_cursor_api_key", return_value="cursor_test"),
    ):
        out = run_local_agent(
            prompt="hello",
            cwd=tmp_path,
            mcp_servers=mcp,
        )

    assert out["ok"] is True
    create.assert_called_once()
    args, kwargs = create.call_args
    assert not kwargs.get("mcp_servers")
    assert len(args) == 1
    options = args[0]
    assert isinstance(options, AgentOptions)
    assert options.mcp_servers is not None
    assert "autorefactor" in options.mcp_servers
    # send should also receive mcp_servers
    send_args = fake_agent.send.call_args
    assert send_args[0][0] == "hello"
    assert send_args[0][1]["mcp_servers"] is mcp
    assert out["duration_s"] is not None
    assert out["duration_s"] >= 0
    assert out.get("timed_out") is False


def test_run_local_agent_timeout_unblocks(tmp_path: Path) -> None:
    pytest.importorskip("cursor_sdk")
    from cursor_sdk import StdioMcpServerConfig

    import threading

    release = threading.Event()

    def _blocking_wait():
        release.wait(timeout=30)
        fake_result = MagicMock()
        fake_result.status = "finished"
        fake_result.result = "late"
        return fake_result

    fake_run = MagicMock()
    fake_run.id = "run-timeout"
    fake_run.wait.side_effect = _blocking_wait
    fake_run.cancel = MagicMock()

    fake_agent = MagicMock()
    fake_agent.agent_id = "agent-timeout"
    fake_agent.send.return_value = fake_run
    fake_agent.__enter__.return_value = fake_agent
    fake_agent.__exit__.return_value = None

    mcp = {
        "autorefactor": StdioMcpServerConfig(
            command="uv",
            args=["run", "autorefactor-mcp"],
            env={"AUTOREFACTOR_REMOTE_URL": "http://127.0.0.1:8765"},
        )
    }

    try:
        with (
            patch("cursor_sdk.Agent.create", return_value=fake_agent),
            patch("ar_eval.secrets.resolve_cursor_api_key", return_value="cursor_test"),
        ):
            out = run_local_agent(
                prompt="hello",
                cwd=tmp_path,
                mcp_servers=mcp,
                timeout_s=0.05,
            )
    finally:
        release.set()

    assert out["ok"] is False
    assert out["status"] == "timeout"
    assert out["timed_out"] is True
    assert out["duration_s"] >= 0.05
    assert out["duration_s"] < 5.0
    fake_run.cancel.assert_called()
