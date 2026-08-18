"""Delete run API + related job cleanup."""

from __future__ import annotations

import json
from pathlib import Path

from autorefactor.http.runs_api import delete_run, resolve_run_dir
from autorefactor.jobs import jobs_dir
from autorefactor.paths import autorefactor_home
from autorefactor.workspace_sync import active_workspace_dir


def test_delete_run_removes_dir_and_jobs_keeps_workspace(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    runs = autorefactor_home() / "runs"
    slug = "demo-aaaaaaaaaa"
    run_dir = runs / slug
    run_dir.mkdir(parents=True)
    (run_dir / "report").mkdir()
    (run_dir / "report" / "metrics.json").write_text("{}", encoding="utf-8")

    active = active_workspace_dir()
    active.mkdir(parents=True)
    (active / "keep.txt").write_text("sync kept", encoding="utf-8")

    jobs_dir().mkdir(parents=True, exist_ok=True)
    job_match = {
        "job_id": "jobmatch0001",
        "status": "done",
        "out": str(run_dir),
        "lock_key": str(run_dir.resolve()),
    }
    job_other = {
        "job_id": "jobother0002",
        "status": "done",
        "out": str(runs / "other-bbbbbbbbbb"),
        "lock_key": str(runs / "other-bbbbbbbbbb"),
    }
    (jobs_dir() / "jobmatch0001.json").write_text(json.dumps(job_match), encoding="utf-8")
    (jobs_dir() / "jobother0002.json").write_text(json.dumps(job_other), encoding="utf-8")

    result = delete_run(slug)
    assert result["ok"] is True
    assert result["deleted_run"] == slug
    assert result["deleted_dir"] is True
    assert "jobmatch0001" in result["deleted_jobs"]
    assert resolve_run_dir(slug) is None
    assert not run_dir.exists()
    assert (jobs_dir() / "jobother0002.json").is_file()
    assert not (jobs_dir() / "jobmatch0001.json").is_file()
    assert (active / "keep.txt").read_text(encoding="utf-8") == "sync kept"


def test_delete_run_matches_slug_segment_and_tmp(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    slug = "proj-cccccccccc"
    run_dir = autorefactor_home() / "runs" / slug
    run_dir.mkdir(parents=True)
    (run_dir / "report").mkdir()

    jobs_dir().mkdir(parents=True, exist_ok=True)
    # Path does not resolve to run_dir (foreign prefix) but contains /runs/{slug}
    foreign = f"/container/.autorefactor/runs/{slug}"
    job = {
        "job_id": "slugjob00001",
        "status": "done",
        "out": foreign,
        "lock_key": foreign,
    }
    (jobs_dir() / "slugjob00001.json").write_text(json.dumps(job), encoding="utf-8")
    (jobs_dir() / "slugjob00001.tmp").write_text("{}", encoding="utf-8")
    (jobs_dir() / "slugjob00001.json.tmp").write_text("{}", encoding="utf-8")

    result = delete_run(slug)
    assert result["ok"] is True
    assert "slugjob00001" in result["deleted_jobs"]
    assert "slugjob00001.tmp" in result["deleted_tmp"]
    assert "slugjob00001.json.tmp" in result["deleted_tmp"]
    assert not (jobs_dir() / "slugjob00001.json").exists()
    assert not (jobs_dir() / "slugjob00001.tmp").exists()
    assert not run_dir.exists()


def test_delete_run_purges_orphan_jobs_when_dir_missing(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    slug = "gone-dddddddddd"
    # No run directory — only orphaned job pointing at slug path
    jobs_dir().mkdir(parents=True, exist_ok=True)
    orphan_path = str(autorefactor_home() / "runs" / slug)
    job = {
        "job_id": "orphanjob001",
        "status": "failed",
        "out": orphan_path,
        "lock_key": orphan_path,
    }
    (jobs_dir() / "orphanjob001.json").write_text(json.dumps(job), encoding="utf-8")

    active = active_workspace_dir()
    active.mkdir(parents=True)
    (active / "keep.txt").write_text("sync kept", encoding="utf-8")

    result = delete_run(slug)
    assert result["ok"] is True
    assert result["deleted_dir"] is False
    assert "orphanjob001" in result["deleted_jobs"]
    assert not (jobs_dir() / "orphanjob001.json").exists()
    assert (active / "keep.txt").read_text(encoding="utf-8") == "sync kept"


def test_delete_run_traversal_rejected(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    result = delete_run("../etc")
    assert result["ok"] is False
    assert result["error"] == "not_found"
