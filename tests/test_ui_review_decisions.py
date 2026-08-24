"""UI review decisions: approve/skip → agent auto_proceed."""

from __future__ import annotations

from pathlib import Path

import pytest

from autorefactor.changesets.models import Changeset, ChangesetStatus, ChangesetTarget
from autorefactor.changesets.store import changeset_needs_hitl, set_user_decision
from autorefactor.http import runs_api
from autorefactor.mcp import tools as mcp_tools
from autorefactor.queue import load_queue, save_queue


def _hitl_cs(**kwargs) -> Changeset:
    defaults = dict(
        id="hitl1",
        title="Risky",
        action="split",
        brief="b",
        rationale="low conf reason",
        confidence=0.25,
        targets=[ChangesetTarget(repo="r", path="a.py", symbol="f", unit_id="u1")],
        status=ChangesetStatus.PENDING,
    )
    defaults.update(kwargs)
    return Changeset(**defaults)


def test_set_user_decision_proceed_keeps_pending(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    save_queue(out, [_hitl_cs()])
    updated = set_user_decision(out, "hitl1", "proceed")
    assert updated.user_decision == "proceed"
    assert updated.status == ChangesetStatus.PENDING
    needs, reason = changeset_needs_hitl(updated, 0.6)
    assert needs is False
    assert reason is None


def test_set_user_decision_skip_marks_skipped(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    save_queue(out, [_hitl_cs()])
    updated = set_user_decision(out, "hitl1", "skip")
    assert updated.user_decision == "skip"
    assert updated.status == ChangesetStatus.SKIPPED


def test_set_user_decision_undo_approve(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    save_queue(out, [_hitl_cs()])
    set_user_decision(out, "hitl1", "proceed")
    cleared = set_user_decision(out, "hitl1", None)
    assert cleared.user_decision is None
    needs, _ = changeset_needs_hitl(cleared, 0.6)
    assert needs is True


def test_apply_changeset_decision_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runs_api, "autorefactor_home", lambda: tmp_path)
    run = tmp_path / "runs" / "ui-run"
    (run / "queue").mkdir(parents=True)
    save_queue(
        run,
        [
            _hitl_cs(
                id="hitl1",
                brief="b",
                rationale="why",
            )
        ],
    )
    result = runs_api.apply_changeset_decision("ui-run", "hitl1", "proceed")
    assert result["ok"] is True
    assert result["changeset"]["user_decision"] == "proceed"
    assert result["changeset"]["needs_review"] is False

    rows = {c["id"]: c for c in runs_api.load_changesets("ui-run") or []}
    assert rows["hitl1"]["user_decision"] == "proceed"
    assert rows["hitl1"]["needs_review"] is False

    undo = runs_api.apply_changeset_decision("ui-run", "hitl1", None)
    assert undo["ok"] is True
    assert undo["changeset"]["user_decision"] is None


def test_get_next_auto_proceeds_ui_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    save_queue(out, [_hitl_cs(confidence=0.2)])
    set_user_decision(out, "hitl1", "proceed")

    monkeypatch.setattr(mcp_tools, "resolve_analyze_workspace", lambda _w=None: tmp_path)
    monkeypatch.setattr(mcp_tools, "resolve_out_dir", lambda _ws, _out=None: out)
    monkeypatch.setattr(
        mcp_tools,
        "load_merged_config",
        lambda **_k: type("C", (), {"confidence_threshold": 0.6})(),
    )
    monkeypatch.setattr(mcp_tools, "_maybe_proxy", lambda *_a, **_k: None)
    monkeypatch.setattr(mcp_tools, "_queue_fetch_baselines", lambda *_a, **_k: {})
    monkeypatch.setattr(mcp_tools, "_verify_workspace_root", lambda _ws: tmp_path)
    monkeypatch.setattr(mcp_tools, "_queue_freshness_gate", lambda **_k: None)

    nxt = mcp_tools.tool_get_next_changeset()
    assert nxt["done"] is False
    assert nxt["auto_proceed"] is True
    assert nxt["needs_confirmation"] is False
    assert nxt["user_decision"] == "proceed"
    assert "UI already approved" in nxt["next_step"]


def test_get_next_approved_only_before_high_conf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autorefactor.mcp import tools as mcp_tools

    out = tmp_path / "out"
    out.mkdir()
    approved = Changeset(
        id="approved_low",
        title="Approved low",
        action="split",
        brief="b",
        rationale="r",
        confidence=0.2,
        user_decision="proceed",
        targets=[ChangesetTarget(repo="r", path="a.java", symbol="A", unit_id="a")],
        status=ChangesetStatus.PENDING,
    )
    high = Changeset(
        id="high_unapproved",
        title="High unapproved",
        action="split",
        brief="b",
        rationale="r",
        confidence=0.95,
        targets=[ChangesetTarget(repo="r", path="b.java", symbol="B", unit_id="b")],
        status=ChangesetStatus.PENDING,
    )
    save_queue(out, [high, approved])

    monkeypatch.setattr(mcp_tools, "resolve_analyze_workspace", lambda _w=None: tmp_path)
    monkeypatch.setattr(mcp_tools, "resolve_out_dir", lambda _ws, _out=None: out)
    monkeypatch.setattr(
        mcp_tools,
        "load_merged_config",
        lambda **_k: type("C", (), {"confidence_threshold": 0.6})(),
    )
    monkeypatch.setattr(mcp_tools, "_maybe_proxy", lambda *_a, **_k: None)
    monkeypatch.setattr(mcp_tools, "_queue_fetch_baselines", lambda *_a, **_k: {})
    monkeypatch.setattr(mcp_tools, "_verify_workspace_root", lambda _ws: tmp_path)
    monkeypatch.setattr(mcp_tools, "_queue_freshness_gate", lambda **_k: None)

    nxt = mcp_tools.tool_get_next_changeset()
    assert nxt["done"] is False
    assert nxt["changeset"]["id"] == "approved_low"
    assert nxt["auto_proceed"] is True


def test_guidance_mentions_queue_ui_review() -> None:
    from autorefactor.config import AutorefactorConfig
    from autorefactor.mcp.guidance import build_agent_guidance, format_mcp_instructions

    cfg = AutorefactorConfig(_env_file=None, api_key="sk-x", chat_backend="agent", embed_backend="local")
    g = build_agent_guidance(cfg)
    after = " ".join(g["workflow"])
    assert "Approve/Skip" in after
    assert "execute" in after.lower()
    text = format_mcp_instructions(g)
    assert "Approve/Skip" in text or "queue UI" in text
