from __future__ import annotations

from pathlib import Path

import pytest

from autorefactor.changesets.models import Changeset, ChangesetStatus
from autorefactor.mcp import tools as mcp_tools
from autorefactor.queue import save_queue


def test_get_next_changeset_autorun_loop_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    save_queue(
        out,
        [
            Changeset(
                id="hi",
                title="high",
                action="split",
                brief="b",
                rationale="r",
                confidence=0.9,
            ),
            Changeset(
                id="lo",
                title="low",
                action="document",
                brief="b",
                rationale="r",
                confidence=0.4,
            ),
        ],
    )
    monkeypatch.setattr(mcp_tools, "resolve_analyze_workspace", lambda _w=None: tmp_path)
    monkeypatch.setattr(mcp_tools, "resolve_out_dir", lambda _ws, _out=None: out)
    monkeypatch.setattr(
        mcp_tools,
        "load_merged_config",
        lambda **_k: type("C", (), {"confidence_threshold": 0.6})(),
    )
    monkeypatch.setattr(mcp_tools, "_maybe_proxy", lambda *_a, **_k: None)

    nxt = mcp_tools.tool_get_next_changeset()
    assert nxt["done"] is False
    assert nxt["auto_proceed"] is True
    assert nxt["loop"] == "autorun"
    assert nxt["queue_counts"]["pending_autorun"] >= 1
    assert nxt["queue_counts"]["pending_hitl"] >= 1
    assert "Do not ask the user" in nxt["next_step"]


def test_mark_done_returns_autorun_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    save_queue(
        out,
        [
            Changeset(
                id="a",
                title="a",
                action="split",
                brief="b",
                rationale="r",
                confidence=0.95,
                status=ChangesetStatus.IN_PROGRESS,
            ),
            Changeset(
                id="b",
                title="b",
                action="split",
                brief="b",
                rationale="r",
                confidence=0.9,
            ),
        ],
    )
    monkeypatch.setattr(mcp_tools, "resolve_analyze_workspace", lambda _w=None: tmp_path)
    monkeypatch.setattr(mcp_tools, "resolve_out_dir", lambda _ws, _out=None: out)
    monkeypatch.setattr(
        mcp_tools,
        "load_merged_config",
        lambda **_k: type("C", (), {"confidence_threshold": 0.6})(),
    )
    monkeypatch.setattr(mcp_tools, "_maybe_proxy", lambda *_a, **_k: None)

    marked = mcp_tools.tool_mark_changeset("a", "done")
    assert marked["ok"] is True
    assert marked["loop"] == "autorun"
    assert "Immediately call get_next_changeset" in marked["next_step"]


def test_get_next_changeset_waiting_deps_not_hitl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    save_queue(
        out,
        [
            Changeset(
                id="split-blocked",
                title="Split later",
                action="split",
                brief="b",
                rationale="r",
                confidence=0.99,
                depends_on=["missing-prep"],
            ),
        ],
    )
    monkeypatch.setattr(mcp_tools, "resolve_analyze_workspace", lambda _w=None: tmp_path)
    monkeypatch.setattr(mcp_tools, "resolve_out_dir", lambda _ws, _out=None: out)
    monkeypatch.setattr(
        mcp_tools,
        "load_merged_config",
        lambda **_k: type("C", (), {"confidence_threshold": 0.6})(),
    )
    monkeypatch.setattr(mcp_tools, "_maybe_proxy", lambda *_a, **_k: None)

    nxt = mcp_tools.tool_get_next_changeset()
    assert nxt["done"] is False
    assert nxt["needs_confirmation"] is False
    assert nxt["auto_proceed"] is False
    assert nxt["loop"] == "waiting"
    assert nxt["queue_counts"]["pending_waiting"] == 1
    assert nxt["queue_counts"]["pending_hitl"] == 0
    assert "waiting on prep" in " ".join(nxt.get("questions") or []).lower() or (
        "waiting" in nxt["next_step"].lower()
    )
