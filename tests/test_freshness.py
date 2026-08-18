"""Workspace content freshness fingerprints."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autorefactor.changesets.models import Changeset
from autorefactor.freshness import freshness_payload, workspace_content_fingerprint
from autorefactor.paths import write_run_identity


def test_fingerprint_stable_then_changes_on_touch(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "src").mkdir()
    src = ws / "src" / "a.py"
    src.write_text("x = 1\n", encoding="utf-8")

    fp1 = workspace_content_fingerprint(ws)
    fp2 = workspace_content_fingerprint(ws)
    assert fp1 == fp2
    assert len(fp1) == 64

    src.write_text("x = 2\n", encoding="utf-8")
    fp3 = workspace_content_fingerprint(ws)
    assert fp3 != fp1


def test_fingerprint_stable_across_mtime_only_touch(tmp_path: Path) -> None:
    """Content fingerprint ignores mtime — analyze/sync re-reads must stay fresh."""
    import os
    import time

    ws = tmp_path / "ws"
    ws.mkdir()
    src = ws / "a.py"
    src.write_text("x = 1\n", encoding="utf-8")
    fp1 = workspace_content_fingerprint(ws)
    time.sleep(0.05)
    os.utime(src, None)
    fp2 = workspace_content_fingerprint(ws)
    assert fp1 == fp2


def test_freshness_true_after_identity_write_false_after_edit(tmp_path: Path) -> None:
    ws = tmp_path / "proj"
    ws.mkdir()
    (ws / "main.py").write_text("print(1)\n", encoding="utf-8")
    out = tmp_path / "run"
    out.mkdir()

    write_run_identity(out, ws)
    identity = json.loads((out / "run_identity.json").read_text(encoding="utf-8"))
    assert identity.get("content_fingerprint")
    assert identity.get("fingerprinted_at")

    fresh = freshness_payload(workspace=ws, out_dir=out)
    assert fresh["fresh"] is True
    assert fresh["freshness"]["reason"] == "ok"

    (ws / "main.py").write_text("print(2)\n", encoding="utf-8")
    stale = freshness_payload(workspace=ws, out_dir=out)
    assert stale["fresh"] is False
    assert stale["freshness"]["reason"] == "content_changed"


def test_missing_fingerprint_is_stale(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("a", encoding="utf-8")
    out = tmp_path / "run"
    out.mkdir()
    (out / "run_identity.json").write_text(
        json.dumps({"label": "ws", "display_name": "ws", "group": "ungrouped"}),
        encoding="utf-8",
    )
    payload = freshness_payload(workspace=ws, out_dir=out)
    assert payload["fresh"] is False
    assert payload["freshness"]["reason"] == "missing_fingerprint"


def test_missing_fingerprint_backfills_when_analyze_artifacts_exist(tmp_path: Path) -> None:
    from autorefactor.changesets.models import ChangesetStatus

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "main.py").write_text("print(1)\n", encoding="utf-8")
    out = tmp_path / "run"
    out.mkdir()
    (out / "run_identity.json").write_text(
        json.dumps({"label": "ws", "display_name": "ws", "group": "ungrouped", "workspace": str(ws)}),
        encoding="utf-8",
    )
    queue = out / "queue" / "changesets.json"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(
        json.dumps(
            {
                "changesets": [
                    {
                        "id": "cs1",
                        "title": "Split",
                        "action": "split",
                        "brief": "b",
                        "rationale": "r",
                        "confidence": 0.9,
                        "targets": [],
                        "status": ChangesetStatus.PENDING.value,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    payload = freshness_payload(workspace=ws, out_dir=out)
    assert payload["fresh"] is True
    assert payload["freshness"]["reason"] == "ok"
    identity = json.loads((out / "run_identity.json").read_text(encoding="utf-8"))
    assert identity.get("content_fingerprint")


def test_fingerprint_ignores_sync_meta(tmp_path: Path) -> None:
    from autorefactor.paths import SYNC_META_NAME

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "main.py").write_text("print(1)\n", encoding="utf-8")
    fp1 = workspace_content_fingerprint(ws)
    (ws / SYNC_META_NAME).write_text(
        '{"label": "loop-retest-janus-bonus", "source": "ar_eval"}\n',
        encoding="utf-8",
    )
    fp2 = workspace_content_fingerprint(ws)
    (ws / SYNC_META_NAME).write_text(
        '{"label": "janus-bonus", "source": "/tmp/janus-bonus"}\n',
        encoding="utf-8",
    )
    fp3 = workspace_content_fingerprint(ws)
    assert fp1 == fp2 == fp3


def test_fingerprint_ignores_eval_snapshot(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "main.py").write_text("print(1)\n", encoding="utf-8")
    fp1 = workspace_content_fingerprint(ws)
    (ws / ".ar_eval_snapshot.json").write_text('{"episode": 1}\n', encoding="utf-8")
    fp2 = workspace_content_fingerprint(ws)
    assert fp1 == fp2


def test_apply_safe_allows_queue_when_harness_snapshot_drifts(tmp_path: Path) -> None:
    from autorefactor.changesets.models import ChangesetStatus
    from autorefactor.mcp.tools import _queue_freshness_gate

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "main.py").write_text("print(1)\n", encoding="utf-8")
    out = tmp_path / "run"
    out.mkdir()
    write_run_identity(out, ws)
    queue = out / "queue" / "changesets.json"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(
        json.dumps(
            {
                "changesets": [
                    {
                        "id": "cs1",
                        "title": "Split",
                        "action": "split",
                        "brief": "b",
                        "rationale": "r",
                        "confidence": 0.9,
                        "targets": [],
                        "status": ChangesetStatus.PENDING.value,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (ws / ".ar_eval_snapshot.json").write_text("{}", encoding="utf-8")
    blocked = _queue_freshness_gate(workspace=ws, out_dir=out, active_job=None)
    assert blocked is None


def test_stale_fingerprint_invalidates_done_skipped_and_surfaces_cta(tmp_path: Path) -> None:
    from autorefactor.changesets.models import ChangesetStatus
    from autorefactor.queue import load_queue, save_queue

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "main.py").write_text("print(1)\n", encoding="utf-8")
    out = tmp_path / "run"
    out.mkdir()
    write_run_identity(out, ws)
    (out / "report").mkdir()
    (out / "report" / "metrics.json").write_text("{}", encoding="utf-8")
    save_queue(
        out,
        [
            Changeset(
                id="done1",
                title="Done split",
                action="split",
                brief="b",
                rationale="r",
                confidence=0.9,
                targets=[],
                status=ChangesetStatus.DONE,
            ),
            Changeset(
                id="skip1",
                title="Skipped doc",
                action="document",
                brief="b",
                rationale="r",
                confidence=0.55,
                targets=[],
                status=ChangesetStatus.SKIPPED,
            ),
        ],
    )
    (ws / "main.py").write_text("print(2)\n", encoding="utf-8")

    stale = freshness_payload(workspace=ws, out_dir=out)
    assert stale["fresh"] is False
    assert stale["apply_safe"] is False
    assert stale["metrics_review_ok"] is True
    assert stale.get("re_analyze_cta")
    assert "Re-analyze" in stale["re_analyze_cta"]
    assert stale.get("stale_queue_invalidated") == 2

    items = load_queue(out)
    assert all(c.status == ChangesetStatus.PENDING for c in items)
    assert all((c.metrics_delta or {}).get("fingerprint_stale") for c in items)


def test_get_status_by_status_matches_post_invalidation_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autorefactor.changesets.models import ChangesetStatus
    from autorefactor.mcp import tools as T
    from autorefactor.mcp.tools import tool_get_status
    from autorefactor.paths import write_run_identity
    from autorefactor.queue import save_queue

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "main.py").write_text("print(1)\n", encoding="utf-8")
    out = tmp_path / "run"
    out.mkdir()
    write_run_identity(out, ws)
    (out / "report").mkdir()
    (out / "report" / "metrics.json").write_text("{}", encoding="utf-8")
    save_queue(
        out,
        [
            Changeset(
                id="done1",
                title="Done split",
                action="split",
                brief="b",
                rationale="r",
                confidence=0.9,
                targets=[],
                status=ChangesetStatus.DONE,
            ),
            Changeset(
                id="skip1",
                title="Skipped doc",
                action="document",
                brief="b",
                rationale="r",
                confidence=0.55,
                targets=[],
                status=ChangesetStatus.SKIPPED,
            ),
        ],
    )
    (ws / "main.py").write_text("print(2)\n", encoding="utf-8")

    monkeypatch.setattr(T, "_maybe_proxy", lambda *_a, **_k: None)
    monkeypatch.setattr(
        T,
        "resolve_workspace_info",
        lambda _w=None: {"workspace": str(ws), "path": ws, "resolved_via": "test"},
    )
    monkeypatch.setattr(T, "resolve_out_dir", lambda _ws, _o=None: out)
    monkeypatch.setattr(
        T,
        "load_merged_config",
        lambda **_k: type(
            "Cfg",
            (),
            {
                "confidence_threshold": 0.6,
                "runtime_summary": lambda self: {"chat_backend": "x", "embed_backend": "y", "parallel_llm": False},
                "resolved_describe_mode": lambda self: "raw",
                "resolved_focus": lambda self: None,
            },
        )(),
    )
    monkeypatch.setattr(T, "needs_setup", lambda _c: False)
    monkeypatch.setattr(T, "setup_hint", lambda _c: None)
    monkeypatch.setattr(
        "autorefactor.enrich_queue.pending_enrich_count", lambda _o: 0
    )

    status = tool_get_status(workspace=str(ws), out=str(out))
    assert status["fresh"] is False
    assert status["stale_queue_invalidated"] == 2
    assert status["fingerprint_stale"] == 2
    assert status["by_status"] == {"pending": 2}
    assert status["queue_counts"]["pending"] == 2
    assert status["queue_counts"]["done"] == 0


def test_apply_safe_false_when_stale_even_with_pending(tmp_path: Path) -> None:
    from autorefactor.changesets.models import ChangesetStatus

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "main.py").write_text("print(1)\n", encoding="utf-8")
    out = tmp_path / "run"
    out.mkdir()
    write_run_identity(out, ws)
    queue = out / "queue" / "changesets.json"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(
        json.dumps(
            {
                "changesets": [
                    {
                        "id": "cs1",
                        "title": "Split",
                        "action": "split",
                        "brief": "b",
                        "rationale": "r",
                        "confidence": 0.9,
                        "targets": [],
                        "status": ChangesetStatus.PENDING.value,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (ws / "main.py").write_text("print(2)\n", encoding="utf-8")
    stale = freshness_payload(workspace=ws, out_dir=out)
    assert stale["fresh"] is False
    assert stale["apply_safe"] is False
