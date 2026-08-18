"""Tests for report UI REST helpers."""

from __future__ import annotations

import json
from pathlib import Path

from autorefactor.http import runs_api


def test_list_runs_and_metrics(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runs_api, "autorefactor_home", lambda: tmp_path)
    run = tmp_path / "runs" / "demo-abc123"
    report = run / "report"
    report.mkdir(parents=True)
    (run / "queue").mkdir()
    metrics = {
        "roi": {
            "debt_score": {"value": 42.0},
            "agent_friction": {"value": 31.0},
        },
        "exact": {"unit_count": 10, "repos_scanned": ["demo"]},
    }
    (report / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    (run / "queue" / "changesets.json").write_text("[]", encoding="utf-8")
    (report / "projection.json").write_text(
        json.dumps({"points": [], "point_count": 0}), encoding="utf-8"
    )
    (run / "run_identity.json").write_text(
        json.dumps(
            {
                "label": "demo-service",
                "display_name": "demo-service",
                "group": "monorepo",
                "source": "/repos/monorepo/demo-service",
                "slug": "demo-abc123",
            }
        ),
        encoding="utf-8",
    )

    rows = runs_api.list_runs()
    assert len(rows) == 1
    assert rows[0]["slug"] == "demo-abc123"
    assert rows[0]["display_name"] == "demo-service"
    assert rows[0]["group"] == "monorepo"
    assert rows[0]["debt_score"] == 42.0
    assert rows[0]["has_projection"] is True

    summary = runs_api.run_summary("demo-abc123")
    assert summary is not None
    assert summary["unit_count"] == 10
    assert summary["display_name"] == "demo-service"
    assert summary["group"] == "monorepo"

    loaded = runs_api.load_run_json("demo-abc123", "report", "metrics.json")
    assert loaded["roi"]["debt_score"]["value"] == 42.0

    with_progress = runs_api.load_metrics_with_progress("demo-abc123")
    assert with_progress is not None
    assert "progress" in with_progress
    assert with_progress["progress"]["at_analyze"]["debt_score"] == 42.0

    missing = runs_api.load_run_json("demo-abc123", "report", "nope.json")
    assert missing is None
    assert runs_api.resolve_run_dir("../etc") is None
    assert runs_api.resolve_run_dir("missing") is None


def test_list_runs_infers_identity_from_sync_meta(tmp_path: Path, monkeypatch) -> None:
    from autorefactor.paths import SYNC_META_NAME

    monkeypatch.setattr(runs_api, "autorefactor_home", lambda: tmp_path)
    ws = tmp_path / "workspaces" / "active"
    ws.mkdir(parents=True)
    (ws / SYNC_META_NAME).write_text(
        json.dumps(
            {
                "label": "billing-service",
                "source": "/home/dev/Git/monorepo/billing-service",
                "kind": "sync",
            }
        ),
        encoding="utf-8",
    )
    run = tmp_path / "runs" / "active-0c874e7322"
    run.mkdir(parents=True)
    (run / "manifest.json").write_text(
        json.dumps({"workspace": str(ws), "repos_scanned": ["active"]}),
        encoding="utf-8",
    )
    rows = runs_api.list_runs()
    assert rows[0]["display_name"] == "billing-service"
    assert rows[0]["group"] == "monorepo"
    assert rows[0]["slug"] == "active-0c874e7322"


def test_projection_404_shape(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runs_api, "autorefactor_home", lambda: tmp_path)
    run = tmp_path / "runs" / "old-run"
    run.mkdir(parents=True)
    assert runs_api.load_run_json("old-run", "report", "projection.json") is None
    assert runs_api.run_summary("old-run")["has_projection"] is False


def test_config_redact(monkeypatch, tmp_path: Path) -> None:
    from autorefactor.config import AutorefactorConfig

    cfg = AutorefactorConfig(
        _env_file=None, api_key="sk-secret", chat_backend="agent", embed_backend="local"
    )
    redacted = runs_api.redact_config(cfg)
    assert redacted["api_key"] == "set"
    assert "sk-secret" not in json.dumps(redacted)

    cfg2 = AutorefactorConfig(
        _env_file=None, api_key=None, chat_backend="agent", embed_backend="local"
    )
    assert runs_api.redact_config(cfg2)["api_key"] == "unset"


def test_cors_headers() -> None:
    h = runs_api.cors_headers("http://localhost:3000")
    assert h["Access-Control-Allow-Origin"] == "http://localhost:3000"
    assert "GET" in h["Access-Control-Allow-Methods"]
    # Do not reflect arbitrary origins
    evil = runs_api.cors_headers("https://evil.example")
    assert evil["Access-Control-Allow-Origin"] != "https://evil.example"
    mut = runs_api.cors_headers("http://localhost:3000", allow_mutations=True)
    assert "PATCH" in mut["Access-Control-Allow-Methods"]
    assert "POST" in mut["Access-Control-Allow-Methods"]


def test_templates() -> None:
    data = runs_api.list_templates()
    assert data["ok"]
    ids = {p["id"] for p in data["templates"]}
    assert "fast_cloud" in ids
    assert "local_agent" in ids
    assert "fully_local" in ids
    assert data["templates"][0].get("chat_backend")
    assert "presets" not in data
