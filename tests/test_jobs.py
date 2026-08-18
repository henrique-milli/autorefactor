from __future__ import annotations

import json
import time
from pathlib import Path

from autorefactor.jobs import (
    job_status,
    jobs_dir,
    list_jobs,
    resolve_job,
    start_analyze_job,
)
from autorefactor.paths import autorefactor_home


def test_background_analyze_job_completes(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    def runner(workspace, out, skip_llm, describe_mode, focus, progress=None):  # noqa: ANN001
        time.sleep(0.05)
        return {"ok": True, "workspace": workspace, "flags": 3, "confirmed": 2}

    started = start_analyze_job(
        workspace="/ws",
        out=None,
        skip_llm=True,
        describe_mode=None,
        focus=None,
        lock_key="/ws",
        runner=runner,
    )
    assert started["ok"] is True
    assert started["status"] == "running"
    job_id = started["job_id"]

    deadline = time.time() + 3
    status = job_status(job_id)
    while status.get("status") == "running" and time.time() < deadline:
        time.sleep(0.05)
        status = job_status(job_id)

    assert status["status"] == "done"
    assert status["result"]["flags"] == 3


def test_second_start_reuses_inflight_job(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    started_runs = {"n": 0}
    release = {"go": False}

    def runner(workspace, out, skip_llm, describe_mode, focus, progress=None):  # noqa: ANN001
        started_runs["n"] += 1
        while not release["go"]:
            time.sleep(0.02)
        return {"ok": True, "flags": 1}

    first = start_analyze_job(
        workspace="/ws",
        out=None,
        skip_llm=True,
        describe_mode=None,
        focus=None,
        lock_key="/tmp/out-a",
        runner=runner,
    )
    second = start_analyze_job(
        workspace="/ws",
        out=None,
        skip_llm=True,
        describe_mode=None,
        focus=None,
        lock_key="/tmp/out-a",
        runner=runner,
    )
    assert first["job_id"] == second["job_id"]
    assert second.get("reused") is True
    assert started_runs["n"] == 1

    release["go"] = True
    deadline = time.time() + 3
    status = job_status(first["job_id"])
    while status.get("status") == "running" and time.time() < deadline:
        time.sleep(0.05)
        status = job_status(first["job_id"])
    assert status["status"] == "done"
    assert started_runs["n"] == 1


def test_job_status_marks_orphan_when_thread_gone(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Ensure autorefactor_home uses the temp HOME
    assert autorefactor_home() == home / ".autorefactor"

    job_id = "orphanjob0001"
    jobs_dir().mkdir(parents=True, exist_ok=True)
    payload = {
        "ok": True,
        "job_id": job_id,
        "status": "running",
        "phase": "embed",
        "percent": 45,
        "started_at": "2026-08-10T08:00:00+00:00",
        "finished_at": None,
        "lock_key": "/tmp/out-orphan",
        "result": None,
        "error": None,
    }
    (jobs_dir() / f"{job_id}.json").write_text(json.dumps(payload), encoding="utf-8")

    status = job_status(job_id)
    assert status["status"] == "failed"
    assert status["error"] == "orphaned"
    assert status.get("finished_at")

    persisted = json.loads((jobs_dir() / f"{job_id}.json").read_text(encoding="utf-8"))
    assert persisted["status"] == "failed"
    assert persisted["error"] == "orphaned"


def test_progress_events_capped_and_status_includes_events(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    release = {"go": False}

    def runner(workspace, out, skip_llm, describe_mode, focus, progress=None):  # noqa: ANN001
        assert progress is not None
        for i in range(40):
            progress("embed", float(i), f"step-{i}")
        while not release["go"]:
            time.sleep(0.02)
        return {"ok": True, "flags": 1}

    started = start_analyze_job(
        workspace="/ws",
        out=None,
        skip_llm=True,
        describe_mode=None,
        focus=None,
        lock_key="/tmp/out-events",
        runner=runner,
    )
    job_id = started["job_id"]
    deadline = time.time() + 3
    status = job_status(job_id)
    while time.time() < deadline:
        events = status.get("events") or []
        if len(events) == 32 and events[-1].get("detail") == "step-39":
            break
        time.sleep(0.02)
        status = job_status(job_id)

    events = status.get("events") or []
    assert len(events) == 32
    assert events[0]["detail"] == "step-8"
    assert events[-1]["detail"] == "step-39"
    assert status.get("updated_at")
    assert status.get("status") == "running"
    assert isinstance(status.get("stale_seconds"), (int, float))

    release["go"] = True
    deadline = time.time() + 3
    while status.get("status") == "running" and time.time() < deadline:
        time.sleep(0.05)
        status = job_status(job_id)
    assert status["status"] == "done"


def test_list_jobs_includes_running_and_resolve_finds_active(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    release = {"go": False}

    def runner(workspace, out, skip_llm, describe_mode, focus, progress=None):  # noqa: ANN001
        if progress:
            progress("sensors", 65, "running sensors")
        while not release["go"]:
            time.sleep(0.02)
        return {"ok": True}

    lock_key = "/tmp/out-list"
    started = start_analyze_job(
        workspace="/ws",
        out=None,
        skip_llm=True,
        describe_mode=None,
        focus=None,
        lock_key=lock_key,
        runner=runner,
    )
    job_id = started["job_id"]
    deadline = time.time() + 3
    listed = list_jobs()
    while not any(j.get("job_id") == job_id for j in listed) and time.time() < deadline:
        time.sleep(0.02)
        listed = list_jobs()

    assert any(j["job_id"] == job_id and j["status"] == "running" for j in listed)

    resolved = resolve_job(lock_key=lock_key)
    assert resolved["ok"] is True
    assert resolved["job_id"] == job_id
    assert resolved["status"] == "running"

    release["go"] = True
    deadline = time.time() + 3
    status = job_status(job_id)
    while status.get("status") == "running" and time.time() < deadline:
        time.sleep(0.05)
        status = job_status(job_id)
    assert status["status"] == "done"


def test_stale_timeout_marks_failed(tmp_path: Path, monkeypatch) -> None:
    from autorefactor import jobs as jobs_mod
    from autorefactor.jobs import job_status, start_analyze_job

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AUTOREFACTOR_JOB_STALE_SECONDS", "1")
    release = {"go": False}

    def runner(workspace, out, skip_llm, describe_mode, focus, progress=None):  # noqa: ANN001
        while not release["go"]:
            time.sleep(0.02)
        return {"ok": True}

    started = start_analyze_job(
        workspace="/ws",
        out=None,
        skip_llm=True,
        describe_mode=None,
        focus=None,
        lock_key="/tmp/out-stale",
        runner=runner,
    )
    job_id = started["job_id"]
    # Backdate timestamps while thread is still alive (bypass _write which stamps updated_at)
    data = jobs_mod.read_job(job_id)
    assert data is not None
    data["started_at"] = "2020-01-01T00:00:00+00:00"
    data["updated_at"] = "2020-01-01T00:00:00+00:00"
    jobs_mod._job_path(job_id).write_text(json.dumps(data), encoding="utf-8")

    status = job_status(job_id)
    assert status["status"] == "failed"
    assert status["error"] == "stale_timeout"

    # Lock freed — a new job can start
    second = start_analyze_job(
        workspace="/ws",
        out=None,
        skip_llm=True,
        describe_mode=None,
        focus=None,
        lock_key="/tmp/out-stale",
        runner=runner,
    )
    assert second["job_id"] != job_id
    release["go"] = True
    deadline = time.time() + 3
    status2 = job_status(second["job_id"])
    while status2.get("status") == "running" and time.time() < deadline:
        time.sleep(0.05)
        status2 = job_status(second["job_id"])
    assert status2["status"] == "done"


def test_cancel_clears_lock_so_second_start_proceeds(tmp_path: Path, monkeypatch) -> None:
    from autorefactor.jobs import cancel_job, job_status, start_analyze_job

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    release = {"go": False}
    runs = {"n": 0}

    def runner(workspace, out, skip_llm, describe_mode, focus, progress=None):  # noqa: ANN001
        runs["n"] += 1
        while not release["go"]:
            time.sleep(0.02)
        return {"ok": True, "n": runs["n"]}

    lock_key = "/tmp/out-cancel"
    first = start_analyze_job(
        workspace="/ws",
        out=None,
        skip_llm=True,
        describe_mode=None,
        focus=None,
        lock_key=lock_key,
        runner=runner,
    )
    assert first["status"] == "running"
    cancelled = cancel_job(first["job_id"])
    assert cancelled["ok"] is True
    assert cancelled["status"] == "cancelled"

    second = start_analyze_job(
        workspace="/ws",
        out=None,
        skip_llm=True,
        describe_mode=None,
        focus=None,
        lock_key=lock_key,
        runner=runner,
    )
    assert second["job_id"] != first["job_id"]
    assert second.get("reused") is not True
    assert second["status"] == "running"

    release["go"] = True
    deadline = time.time() + 3
    status = job_status(second["job_id"])
    while status.get("status") == "running" and time.time() < deadline:
        time.sleep(0.05)
        status = job_status(second["job_id"])
    assert status["status"] == "done"
    assert runs["n"] >= 2
