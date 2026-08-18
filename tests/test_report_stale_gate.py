from __future__ import annotations

import json
from pathlib import Path

import pytest

from autorefactor.mcp import tools as mcp_tools
from autorefactor.paths import write_run_identity


def _seed_report(out: Path) -> None:
    report = out / "report"
    report.mkdir(parents=True)
    (report / "report.md").write_text("# Report\nhello\n", encoding="utf-8")
    (report / "metrics.json").write_text(
        json.dumps({"impact": {"headline": "h", "pitch": "p", "upside_bullets": []}}),
        encoding="utf-8",
    )


def _patch_ws(monkeypatch: pytest.MonkeyPatch, ws: Path, out: Path) -> None:
    info = {
        "path": ws,
        "workspace": str(ws),
        "resolved_via": "explicit",
        "sync_source": None,
        "sync_label": None,
    }
    monkeypatch.setattr(mcp_tools, "resolve_workspace_info", lambda _w=None: info)
    monkeypatch.setattr(mcp_tools, "resolve_analyze_workspace", lambda _w=None: ws)
    monkeypatch.setattr(mcp_tools, "resolve_out_dir", lambda _ws, _o=None: out)
    monkeypatch.setattr(mcp_tools, "_maybe_proxy", lambda *_a, **_k: None)


def test_get_report_returns_stale_banner_without_allow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("x=1\n", encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    write_run_identity(out, ws)
    _seed_report(out)
    (ws / "a.py").write_text("x=2\n", encoding="utf-8")

    _patch_ws(monkeypatch, ws, out)

    stale = mcp_tools.tool_get_report()
    assert stale.get("ok") is not False
    assert stale["stale"] is True
    assert "metrics" in stale
    assert "report_excerpt" in stale
    banner = stale.get("stale_banner") or ""
    assert "STALE" in banner.upper()
    assert "STALE" in (stale.get("report_excerpt") or "").upper()

    allowed = mcp_tools.tool_get_report(allow_stale=True)
    assert allowed.get("ok") is not False
    assert allowed["stale"] is True
    assert allowed.get("allow_stale") is True
    assert "metrics" in allowed


def test_get_report_ok_when_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("x=1\n", encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    write_run_identity(out, ws)
    _seed_report(out)

    _patch_ws(monkeypatch, ws, out)

    payload = mcp_tools.tool_get_report()
    assert payload.get("ok", True) is not False
    assert payload.get("stale") is not True
    assert payload["fresh"] is True
    assert "metrics" in payload
