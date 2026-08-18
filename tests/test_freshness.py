"""Workspace content freshness fingerprints."""

from __future__ import annotations

import json
from pathlib import Path

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
