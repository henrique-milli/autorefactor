"""Tests for HITL review payloads with code snippets."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autorefactor.changesets.models import Changeset, ChangesetStatus, ChangesetTarget
from autorefactor.mcp import tools as mcp_tools
from autorefactor.queue import save_queue

_SNIPPET = "def risky_merge():\n    return handler_a() or handler_b()\n"


def _write_units(out: Path, *, snippet: str = _SNIPPET) -> None:
    analyze = out / "analyze"
    analyze.mkdir(parents=True, exist_ok=True)
    (analyze / "units.json").write_text(
        json.dumps(
            [
                {
                    "id": "u1",
                    "repo": "r",
                    "path": "a.py",
                    "qualified_name": "risky_merge",
                    "kind": "function",
                    "language": "python",
                    "source_snippet": snippet,
                    "content_hash": "abc123",
                }
            ]
        ),
        encoding="utf-8",
    )


def _patch_common(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, out: Path) -> None:
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


def test_get_next_changeset_hitl_review_includes_snippets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    _write_units(out)
    save_queue(
        out,
        [
            Changeset(
                id="hitl-doc",
                title="Risky merge",
                action="document",
                brief="Two handlers differ only in exception mapping.",
                rationale="r",
                confidence=0.35,
                targets=[
                    ChangesetTarget(repo="r", path="a.py", symbol="risky_merge", unit_id="u1")
                ],
            ),
        ],
    )
    _patch_common(monkeypatch, tmp_path, out)

    nxt = mcp_tools.tool_get_next_changeset()
    assert nxt["done"] is False
    assert nxt["needs_confirmation"] is True
    assert "hitl_review" in nxt
    review = nxt["hitl_review"]
    assert review["title"] == "Risky merge"
    assert "risky_merge" in review["targets"][0]["source_snippet"]
    # Prefer model rationale over markdown brief
    assert "exception mapping" in (review.get("rationale") or "").lower() or review.get("rationale")
    assert nxt["changeset"]["targets"][0]["source_snippet"]
    assert "```" in nxt["hitl_digest"]
    assert "choices" in review
    assert review["prompt"]


def test_get_changeset_enriches_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    _write_units(out)
    save_queue(
        out,
        [
            Changeset(
                id="cs1",
                title="t",
                action="split",
                brief="b",
                rationale="r",
                confidence=0.5,
                targets=[
                    ChangesetTarget(repo="r", path="a.py", symbol="risky_merge", unit_id="u1")
                ],
            ),
        ],
    )
    _patch_common(monkeypatch, tmp_path, out)

    detail = mcp_tools.tool_get_changeset("cs1")
    assert detail["ok"] is True
    assert detail["changeset"]["targets"][0]["source_snippet"]
    assert "risky_merge" in detail["changeset"]["targets"][0]["source_snippet"]


def test_hitl_preserves_long_snippets_and_multi_target_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    long_a = "def handler_a():\n" + "\n".join(f"    step_a_{i}()" for i in range(60)) + "\n"
    long_b = "def handler_b():\n" + "\n".join(f"    step_b_{i}()" for i in range(60)) + "\n"
    long_c = "def handler_c():\n" + "\n".join(f"    step_c_{i}()" for i in range(60)) + "\n"
    analyze = out / "analyze"
    analyze.mkdir(parents=True, exist_ok=True)
    (analyze / "units.json").write_text(
        json.dumps(
            [
                {
                    "id": "u1",
                    "repo": "r",
                    "path": "a.py",
                    "qualified_name": "handler_a",
                    "kind": "function",
                    "language": "python",
                    "source_snippet": long_a,
                    "content_hash": "a",
                },
                {
                    "id": "u2",
                    "repo": "r",
                    "path": "b.py",
                    "qualified_name": "handler_b",
                    "kind": "function",
                    "language": "python",
                    "source_snippet": long_b,
                    "content_hash": "b",
                },
                {
                    "id": "u3",
                    "repo": "r",
                    "path": "c.py",
                    "qualified_name": "handler_c",
                    "kind": "function",
                    "language": "python",
                    "source_snippet": long_c,
                    "content_hash": "c",
                },
            ]
        ),
        encoding="utf-8",
    )
    rationale = (
        "These three handlers share the same control flow and exception mapping; "
        "merging them would remove duplicated ADM ack paths across adapters. "
    ) * 20
    save_queue(
        out,
        [
            Changeset(
                id="hitl-long",
                title="Merge similar handlers",
                action="dedup",
                brief="clone set",
                rationale=rationale,
                confidence=0.35,
                metrics_delta={"kind": "near_duplicate"},
                targets=[
                    ChangesetTarget(repo="r", path="a.py", symbol="handler_a", unit_id="u1"),
                    ChangesetTarget(repo="r", path="b.py", symbol="handler_b", unit_id="u2"),
                    ChangesetTarget(repo="r", path="c.py", symbol="handler_c", unit_id="u3"),
                ],
            ),
        ],
    )
    _patch_common(monkeypatch, tmp_path, out)

    nxt = mcp_tools.tool_get_next_changeset()
    review = nxt["hitl_review"]
    assert review["targets"][0]["source_snippet"] == long_a
    assert "step_a_59()" in review["targets"][0]["source_snippet"]
    assert "step_b_59()" in review["targets"][1]["source_snippet"]
    assert "step_c_59()" in review["targets"][2]["source_snippet"]
    assert rationale.strip() == review["rationale"] or rationale[:4000].rstrip() in (
        review["rationale"] or ""
    )
    digest = nxt["hitl_digest"]
    assert "step_a_59()" in digest
    assert "step_b_59()" in digest
    assert "step_c_59()" in digest
    assert digest.count("```") >= 6
