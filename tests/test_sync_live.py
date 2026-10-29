"""Live sync proof against a running autorefactor engine (optional)."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("AUTOREFACTOR_REMOTE_URL"),
    reason="Set AUTOREFACTOR_REMOTE_URL to run live sync proof",
)


def _http_json(url: str, *, timeout: float = 30.0) -> dict:
    import urllib.request

    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_live_sync_uploads_marker_file(tmp_path: Path, monkeypatch) -> None:
    """Pack+POST via MCP proxy; verify marker appears on remote active workspace."""
    from autorefactor.mcp.remote import proxy_tool, reset_proxy_state

    reset_proxy_state()
    monkeypatch.chdir(Path.home())
    monkeypatch.delenv("AUTOREFACTOR_MCP_WORKSPACE", raising=False)

    marker = f"sync-proof-{uuid.uuid4().hex[:8]}.txt"
    proj = tmp_path / "sync-proof-proj"
    proj.mkdir()
    (proj / "package.json").write_text('{"name":"sync-proof"}\n', encoding="utf-8")
    (proj / marker).write_text("sync-ok\n", encoding="utf-8")

    result = proxy_tool("sync_workspace", {"path": str(proj)})
    assert result.get("ok") is True, result
    assert result.get("remote_workspace") == "active"
    assert int(result.get("uploaded_bytes") or result.get("pack", {}).get("bytes") or 0) > 0
    assert result.get("source") == str(proj.resolve()) or result.get("local_root") == str(
        proj.resolve()
    )

    base = os.environ["AUTOREFACTOR_REMOTE_URL"].rstrip("/").removesuffix("/mcp")
    remote = _http_json(f"{base}/v1/workspace")
    assert remote.get("source") == str(proj.resolve())
    assert remote.get("synced") is True
    assert int(remote.get("file_count") or 0) >= 2

    reset_proxy_state()


def test_live_product_repo_uses_mounted_without_upload(monkeypatch) -> None:
    """Autorefactor product checkout skips tar upload and targets mounted workspace."""
    from autorefactor.mcp.remote import proxy_tool, reset_proxy_state

    repo = Path(__file__).resolve().parents[1]
    if not (repo / "src" / "autorefactor").is_dir():
        pytest.skip("not in autorefactor product repo")

    reset_proxy_state()
    result = proxy_tool("sync_workspace", {"path": str(repo)})
    assert result.get("ok") is True, result
    assert result.get("skipped") is True
    assert result.get("reason") == "product_repo_mounted"
    assert result.get("remote_workspace") == "mounted"

    reset_proxy_state()
