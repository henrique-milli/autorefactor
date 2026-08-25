"""Background analyze jobs for MCP / remote (avoids client tool timeouts)."""

from __future__ import annotations

import json
import os
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from autorefactor.paths import autorefactor_home

_LOCK = threading.Lock()
_THREADS: dict[str, threading.Thread] = {}
_ACTIVE_BY_KEY: dict[str, str] = {}  # lock_key -> job_id
_CANCELLED: set[str] = set()

AnalyzeRunner = Callable[..., dict[str, Any]]

_MAX_EVENTS = 32
_DEFAULT_STALE_SECONDS = 1800.0


def stale_job_seconds() -> float:
    raw = (os.environ.get("AUTOREFACTOR_JOB_STALE_SECONDS") or "").strip()
    if not raw:
        return _DEFAULT_STALE_SECONDS
    try:
        return max(1.0, float(raw))
    except ValueError:
        return _DEFAULT_STALE_SECONDS


def jobs_dir() -> Path:
    path = autorefactor_home() / "jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _job_path(job_id: str) -> Path:
    return jobs_dir() / f"{job_id}.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _stale_seconds(data: dict[str, Any]) -> float | None:
    if data.get("status") != "running":
        return None
    updated = _parse_iso(data.get("updated_at")) or _parse_iso(data.get("started_at"))
    if updated is None:
        return None
    now = datetime.now(timezone.utc)
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return max(0.0, (now - updated).total_seconds())


def _append_event(
    state: dict[str, Any],
    *,
    phase: str,
    percent: float | None,
    detail: str | None,
) -> None:
    events = state.get("events")
    if not isinstance(events, list):
        events = []
    events.append(
        {
            "ts": _now(),
            "phase": phase,
            "percent": percent if percent is not None else state.get("percent"),
            "detail": detail,
        }
    )
    if len(events) > _MAX_EVENTS:
        events = events[-_MAX_EVENTS:]
    state["events"] = events


def _write(job_id: str, payload: dict[str, Any]) -> None:
    payload["updated_at"] = _now()
    path = _job_path(job_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def read_job(job_id: str) -> dict[str, Any] | None:
    path = _job_path(job_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def active_job_id(lock_key: str) -> str | None:
    """Return job_id holding ``lock_key`` if its thread is still alive."""
    with _LOCK:
        job_id = _ACTIVE_BY_KEY.get(lock_key)
        if not job_id:
            return None
        thread = _THREADS.get(job_id)
        if thread is not None and thread.is_alive():
            return job_id
        return None


def _thread_alive(job_id: str) -> bool:
    with _LOCK:
        thread = _THREADS.get(job_id)
    return thread is not None and thread.is_alive()


def _clear_lock(job_id: str, data: dict[str, Any]) -> None:
    lock_key = data.get("lock_key")
    with _LOCK:
        _THREADS.pop(job_id, None)
        if isinstance(lock_key, str) and _ACTIVE_BY_KEY.get(lock_key) == job_id:
            _ACTIVE_BY_KEY.pop(lock_key, None)


def _reconcile_orphan(job_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """If status is running but no live thread, mark failed/orphaned and persist."""
    if data.get("status") != "running":
        return data
    if _thread_alive(job_id):
        return data
    # Thread may have just finished — re-read before declaring orphan (avoid TOCTOU).
    fresh = read_job(job_id)
    if fresh is not None and fresh.get("status") != "running":
        return fresh
    data["status"] = "failed"
    data["phase"] = "failed"
    data["error"] = "orphaned"
    data["finished_at"] = _now()
    data["result"] = {
        "ok": False,
        "error": "orphaned",
        "detail": "Analyze job thread is no longer running (process restart or crash during analyze).",
    }
    _write(job_id, data)
    _clear_lock(job_id, data)
    return data


def _reconcile_stale(job_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """If running longer than stale timeout, mark failed and free the lock."""
    if data.get("status") != "running":
        return data
    stale = _stale_seconds(data)
    if stale is None or stale <= stale_job_seconds():
        return data
    data["status"] = "failed"
    data["phase"] = "failed"
    data["error"] = "stale_timeout"
    data["finished_at"] = _now()
    data["result"] = {
        "ok": False,
        "error": "stale_timeout",
        "detail": f"Analyze job exceeded stale timeout ({int(stale_job_seconds())}s).",
        "stale_seconds": stale,
    }
    _write(job_id, data)
    with _LOCK:
        _CANCELLED.add(job_id)
    _clear_lock(job_id, data)
    return data


def _reconcile(job_id: str, data: dict[str, Any]) -> dict[str, Any]:
    data = _reconcile_orphan(job_id, data)
    return _reconcile_stale(job_id, data)


def _status_payload(job_id: str, data: dict[str, Any], *, reused: bool = False) -> dict[str, Any]:
    events = data.get("events") if isinstance(data.get("events"), list) else []
    out: dict[str, Any] = {
        "ok": True,
        "job_id": job_id,
        "status": data.get("status"),
        "phase": data.get("phase"),
        "percent": data.get("percent"),
        "detail": data.get("detail"),
        "started_at": data.get("started_at"),
        "finished_at": data.get("finished_at"),
        "updated_at": data.get("updated_at"),
        "error": data.get("error"),
        "workspace": data.get("workspace"),
        "out": data.get("out"),
        "slug": Path(str(data.get("out") or "")).name or None,
        "lock_key": data.get("lock_key"),
        "describe_mode": data.get("describe_mode"),
        "focus": data.get("focus"),
        "events": events,
        "background": True,
        "poll": "get_progress",
    }
    stale = _stale_seconds(data)
    if stale is not None:
        out["stale_seconds"] = stale
    if reused:
        out["reused"] = True
    if data.get("status") == "done" and isinstance(data.get("result"), dict):
        out["result"] = data["result"]
    elif data.get("status") in {"failed", "cancelled"}:
        out["result"] = data.get("result")
    return out


def _job_summary(job_id: str, data: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "job_id": job_id,
        "status": data.get("status"),
        "phase": data.get("phase"),
        "percent": data.get("percent"),
        "detail": data.get("detail"),
        "started_at": data.get("started_at"),
        "updated_at": data.get("updated_at"),
        "finished_at": data.get("finished_at"),
        "workspace": data.get("workspace"),
        "out": data.get("out"),
        "lock_key": data.get("lock_key"),
        "error": data.get("error"),
    }
    stale = _stale_seconds(data)
    if stale is not None:
        summary["stale_seconds"] = stale
    return summary


def list_jobs(*, limit: int = 20, status: str | None = None) -> list[dict[str, Any]]:
    """Scan job files, reconcile orphans, return compact summaries (newest first)."""
    paths = sorted(jobs_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[dict[str, Any]] = []
    for path in paths:
        if path.suffix != ".json" or path.name.endswith(".tmp"):
            continue
        job_id = path.stem
        data = read_job(job_id)
        if data is None:
            continue
        data = _reconcile(job_id, data)
        if status is not None and data.get("status") != status:
            continue
        out.append(_job_summary(job_id, data))
        if len(out) >= max(1, limit):
            break
    out.sort(key=lambda j: j.get("started_at") or "", reverse=True)
    return out


def resolve_job(
    *,
    job_id: str | None = None,
    lock_key: str | None = None,
) -> dict[str, Any]:
    """Resolve a job by id, else active for lock_key, else most recent for key / overall."""
    if job_id:
        return job_status(job_id)

    if lock_key:
        active = active_job_id(lock_key)
        if active:
            return job_status(active)
        for summary in list_jobs(limit=50):
            if summary.get("lock_key") == lock_key:
                return job_status(str(summary["job_id"]))
        return {"ok": False, "error": "no_job", "lock_key": lock_key}

    active_ids: list[str] = []
    with _LOCK:
        for jid, thread in _THREADS.items():
            if thread.is_alive():
                active_ids.append(jid)
    if active_ids:
        # Prefer the most recently started among live threads
        best: dict[str, Any] | None = None
        for jid in active_ids:
            payload = job_status(jid)
            if not payload.get("ok"):
                continue
            if best is None or (payload.get("started_at") or "") > (best.get("started_at") or ""):
                best = payload
        if best is not None:
            return best

    listed = list_jobs(limit=1)
    if not listed:
        return {"ok": False, "error": "no_job"}
    return job_status(str(listed[0]["job_id"]))


def start_analyze_job(
    *,
    workspace: str | None,
    out: str | None,
    skip_llm: bool,
    describe_mode: str | None,
    focus: str | None,
    runner: AnalyzeRunner,
    lock_key: str,
) -> dict[str, Any]:
    """Spawn a daemon thread that calls ``runner`` and persists status + progress.

    Only one job may run per ``lock_key`` (typically resolved out_dir). A second
    start for the same key reuses the in-flight job_id.
    """
    with _LOCK:
        existing_id = _ACTIVE_BY_KEY.get(lock_key)
        if existing_id:
            thread = _THREADS.get(existing_id)
            if thread is not None and thread.is_alive():
                data = read_job(existing_id) or {
                    "job_id": existing_id,
                    "status": "running",
                    "phase": "starting",
                    "percent": 0,
                    "started_at": None,
                }
                return _status_payload(existing_id, data, reused=True)
            _ACTIVE_BY_KEY.pop(lock_key, None)
            _THREADS.pop(existing_id, None)

    job_id = uuid.uuid4().hex[:12]
    state: dict[str, Any] = {
        "ok": True,
        "job_id": job_id,
        "status": "running",
        "phase": "starting",
        "percent": 0,
        "detail": None,
        "started_at": _now(),
        "finished_at": None,
        "workspace": workspace,
        "out": out,
        "lock_key": lock_key,
        "skip_llm": skip_llm,
        "describe_mode": describe_mode,
        "focus": focus,
        "result": None,
        "error": None,
        "events": [],
    }
    _append_event(state, phase="starting", percent=0, detail=None)
    _write(job_id, state)

    def progress(phase: str, percent: float | None = None, detail: str | None = None) -> None:
        state["phase"] = phase
        if percent is not None:
            state["percent"] = percent
        if detail is not None:
            state["detail"] = detail
        _append_event(
            state,
            phase=phase,
            percent=percent if percent is not None else state.get("percent"),
            detail=detail,
        )
        _write(job_id, state)

    def _was_cancelled() -> bool:
        with _LOCK:
            return job_id in _CANCELLED

    def _apply_result(result: dict[str, Any]) -> None:
        if _was_cancelled():
            state["status"] = "cancelled"
            state["phase"] = "cancelled"
            state["error"] = state.get("error") or "cancelled"
            return
        state["status"] = "done" if result.get("ok", True) else "failed"
        state["phase"] = "done" if result.get("ok", True) else "failed"
        state["percent"] = 100 if result.get("ok", True) else state.get("percent")
        state["result"] = result
        if not result.get("ok", True):
            state["error"] = result.get("error") or result.get("detail")

    def _run() -> None:
        try:
            result = runner(
                workspace,
                out,
                skip_llm,
                describe_mode,
                focus,
                progress,
            )
            _apply_result(result)
        except TypeError:
            # Older runners without progress arg
            try:
                result = runner(workspace, out, skip_llm, describe_mode, focus)
                _apply_result(result)
            except Exception as exc:  # noqa: BLE001
                if not _was_cancelled():
                    state["status"] = "failed"
                    state["phase"] = "failed"
                    state["error"] = str(exc)[:800]
                    state["result"] = {
                        "ok": False,
                        "error": "analyze_failed",
                        "detail": str(exc)[:800],
                        "traceback": traceback.format_exc()[-2000:],
                    }
        except Exception as exc:  # noqa: BLE001
            if not _was_cancelled():
                state["status"] = "failed"
                state["phase"] = "failed"
                state["error"] = str(exc)[:800]
                state["result"] = {
                    "ok": False,
                    "error": "analyze_failed",
                    "detail": str(exc)[:800],
                    "traceback": traceback.format_exc()[-2000:],
                }
        finally:
            if _was_cancelled() and state.get("status") == "running":
                state["status"] = "cancelled"
                state["phase"] = "cancelled"
                state["error"] = "cancelled"
            state["finished_at"] = state.get("finished_at") or _now()
            _write(job_id, state)
            with _LOCK:
                _CANCELLED.discard(job_id)
                _THREADS.pop(job_id, None)
                if _ACTIVE_BY_KEY.get(lock_key) == job_id:
                    _ACTIVE_BY_KEY.pop(lock_key, None)

    thread = threading.Thread(target=_run, name=f"autorefactor-analyze-{job_id}", daemon=True)
    with _LOCK:
        # Double-check after building state in case of race
        existing_id = _ACTIVE_BY_KEY.get(lock_key)
        if existing_id and existing_id != job_id:
            existing_thread = _THREADS.get(existing_id)
            if existing_thread is not None and existing_thread.is_alive():
                try:
                    _job_path(job_id).unlink(missing_ok=True)
                except OSError:
                    pass
                data = read_job(existing_id) or {"job_id": existing_id, "status": "running"}
                return _status_payload(existing_id, data, reused=True)
        _THREADS[job_id] = thread
        _ACTIVE_BY_KEY[lock_key] = job_id
    thread.start()
    return _status_payload(job_id, state)


def job_status(job_id: str) -> dict[str, Any]:
    data = read_job(job_id)
    if data is None:
        return {"ok": False, "error": "unknown_job", "job_id": job_id}
    data = _reconcile(job_id, data)
    return _status_payload(job_id, data)


def cancel_job(job_id: str) -> dict[str, Any]:
    """Mark a running job cancelled and free its lock so a new analyze can start."""
    data = read_job(job_id)
    if data is None:
        return {"ok": False, "error": "unknown_job", "job_id": job_id}
    data = _reconcile(job_id, data)
    if data.get("status") != "running":
        return {
            "ok": False,
            "error": "not_running",
            "job_id": job_id,
            "status": data.get("status"),
        }
    data["status"] = "cancelled"
    data["phase"] = "cancelled"
    data["error"] = "cancelled"
    data["finished_at"] = _now()
    data["result"] = {
        "ok": False,
        "error": "cancelled",
        "detail": "Analyze job cancelled.",
    }
    _write(job_id, data)
    with _LOCK:
        _CANCELLED.add(job_id)
    _clear_lock(job_id, data)
    return _status_payload(job_id, data)


def delete_job_file(job_id: str) -> bool:
    """Delete a job JSON file and clear in-memory bookkeeping. Returns True if removed."""
    data = read_job(job_id)
    path = _job_path(job_id)
    removed = False
    if path.is_file():
        try:
            path.unlink()
            removed = True
        except OSError:
            removed = False
    if data is not None:
        with _LOCK:
            _CANCELLED.add(job_id)
        _clear_lock(job_id, data)
        with _LOCK:
            _CANCELLED.discard(job_id)
    else:
        with _LOCK:
            _THREADS.pop(job_id, None)
            _CANCELLED.discard(job_id)
    return removed


def _path_points_at_run(raw: str, run_dir: Path, slug: str | None) -> bool:
    """True when a job path field refers to ``run_dir`` or ``slug`` under runs/."""
    text = raw.strip().replace("\\", "/")
    if not text:
        return False
    target = Path(run_dir).expanduser()
    try:
        target_resolved = target.resolve()
    except OSError:
        target_resolved = target
    target_s = str(target_resolved).rstrip("/")
    try:
        if Path(text).expanduser().resolve() == target_resolved:
            return True
    except OSError:
        pass
    if text.rstrip("/") == target_s or text.rstrip("/").endswith(f"/{target.name}"):
        return True
    if slug:
        marker = f"/runs/{slug}"
        if marker in text or text.rstrip("/").endswith(f"/{slug}"):
            return True
        try:
            if Path(text).name == slug:
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


def jobs_pointing_at_run(run_dir: Path, *, slug: str | None = None) -> list[str]:
    """Job ids whose ``out`` / ``lock_key`` points at ``run_dir`` (or ``slug``)."""
    slug = slug or Path(run_dir).name
    matched: list[str] = []
    seen: set[str] = set()
    for path in jobs_dir().glob("*.json"):
        # Skip write sidecars named like foo.json.tmp (suffix .tmp, not .json)
        if path.name.endswith(".tmp"):
            continue
        job_id = path.stem
        data = read_job(job_id)
        hit = False
        if data is None:
            # Corrupt / unreadable: still match if raw file text mentions the slug path
            try:
                raw_text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                raw_text = ""
            if slug and (f"/runs/{slug}" in raw_text.replace("\\", "/") or f'"/{slug}"' in raw_text):
                hit = True
        else:
            for key in ("out", "lock_key"):
                raw = data.get(key)
                if isinstance(raw, str) and _path_points_at_run(raw, run_dir, slug):
                    hit = True
                    break
        if hit and job_id not in seen:
            seen.add(job_id)
            matched.append(job_id)
    return matched


def delete_jobs_for_run(run_dir: Path, slug: str) -> dict[str, Any]:
    """Cancel running jobs for a run, then delete job JSON + ``.tmp`` sidecars."""
    deleted_jobs: list[str] = []
    deleted_tmp: list[str] = []
    for job_id in jobs_pointing_at_run(run_dir, slug=slug):
        data = read_job(job_id)
        if data is not None and data.get("status") == "running":
            cancel_job(job_id)
        if delete_job_file(job_id):
            deleted_jobs.append(job_id)
        # _write uses path.with_suffix(".tmp") → ``{id}.tmp``; also clear ``{id}.json.tmp``
        for name in (f"{job_id}.tmp", f"{job_id}.json.tmp"):
            tmp = jobs_dir() / name
            if tmp.is_file():
                try:
                    tmp.unlink()
                    deleted_tmp.append(name)
                except OSError:
                    pass
    return {"deleted_jobs": deleted_jobs, "deleted_tmp": deleted_tmp}
