from __future__ import annotations

from pathlib import Path

import pytest

from autorefactor.changesets.models import Changeset, ChangesetStatus, ChangesetTarget
from autorefactor.config import AutorefactorConfig
from autorefactor.mcp import tools as T
from autorefactor.mcp.tools import tool_get_status, tool_sample_queue
from autorefactor.queue import load_queue, save_queue


def _cfg(**kwargs) -> AutorefactorConfig:
    kwargs.setdefault("api_key", "sk-x")
    kwargs.setdefault("confidence_threshold", 0.6)
    return AutorefactorConfig(_env_file=None, **kwargs)


def _cs(
    *,
    cid: str,
    title: str,
    action: str,
    confidence: float,
    status: ChangesetStatus = ChangesetStatus.PENDING,
) -> Changeset:
    return Changeset(
        id=cid,
        title=title,
        action=action,
        brief=f"brief for {cid}",
        rationale="r",
        confidence=confidence,
        targets=[ChangesetTarget(repo="r", path="a.py", symbol="f", unit_id="u1")],
        status=status,
    )


def _patch_ws(monkeypatch: pytest.MonkeyPatch, ws: Path, out: Path) -> None:
    monkeypatch.setattr(T, "_maybe_proxy", lambda *_a, **_k: None)
    monkeypatch.setattr(
        T,
        "resolve_workspace_info",
        lambda _w=None: {
            "workspace": str(ws),
            "path": ws,
            "resolved_via": "test",
        },
    )
    monkeypatch.setattr(T, "resolve_out_dir", lambda _ws, _o=None: out)
    monkeypatch.setattr(
        T,
        "load_merged_config",
        lambda **_k: _cfg(workspace=ws, out_dir=out),
    )


def test_sample_queue_splits_auto_and_hitl_without_mutating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    save_queue(
        out,
        [
            _cs(cid="auto1", title="Split helper", action="split", confidence=0.92),
            _cs(cid="auto2", title="Extract shared", action="extract_shared", confidence=0.81),
            _cs(cid="hitl1", title="Risky merge", action="split", confidence=0.21),
            _cs(cid="hitl2", title="Low split", action="split", confidence=0.35),
            _cs(cid="hitl3", title="Docs", action="document", confidence=0.9),
            _cs(cid="done1", title="Done", action="split", confidence=0.99, status=ChangesetStatus.DONE),
        ],
    )
    before = [c.status for c in load_queue(out)]
    _patch_ws(monkeypatch, ws, out)

    sampled = tool_sample_queue(out=str(out), workspace=str(ws), hitl_limit=2)
    assert sampled["ok"] is True
    assert sampled["auto_sample"][0]["id"] == "auto1"
    assert sampled["auto_sample"][0]["auto_proceed"] is True
    hitl_ids = [i["id"] for i in sampled["hitl_sample"]]
    assert hitl_ids == ["hitl1", "hitl2"]
    assert all(i["needs_confirmation"] for i in sampled["hitl_sample"])
    assert sampled["hitl_burden"]["pending_hitl"] >= 2
    assert sampled["hitl_burden"]["pending_autorun"] >= 2
    assert 0 <= sampled["hitl_burden"]["ratio"] <= 1
    assert "hitl_digest" in sampled["hitl_sample"][0]
    after = [c.status for c in load_queue(out)]
    assert after == before


def test_get_status_includes_hitl_burden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "main.py").write_text("print(1)\n", encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    (out / "queue").mkdir()
    save_queue(
        out,
        [
            _cs(cid="auto1", title="Split", action="split", confidence=0.9),
            _cs(cid="hitl1", title="Low", action="split", confidence=0.2),
        ],
    )
    _patch_ws(monkeypatch, ws, out)
    status = tool_get_status(workspace=str(ws), out=str(out))
    assert status["ok"] is True
    assert "hitl_burden" in status
    assert status["hitl_burden"]["pending"] == 2
    assert status["hitl_burden"]["pending_hitl"] >= 1
    assert status["queue_counts"]["pending"] == 2
