"""Shared autorefactor engine helpers: health, sync, analyze, optional container restart."""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ar_eval.timing import record_duration

AnalyzeProgress = Callable[[dict[str, Any]], None]


DEFAULT_ENGINE = "http://127.0.0.1:8765"


def engine_base(url: str | None = None) -> str:
    return (url or DEFAULT_ENGINE).rstrip("/")


def health(url: str | None = None, *, timeout: float = 10.0) -> dict[str, Any]:
    base = engine_base(url)
    req = urllib.request.Request(f"{base}/health", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body) if body else {"ok": True}
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise RuntimeError(f"engine health failed at {base}/health: {exc}") from exc


def wait_healthy(
    url: str | None = None,
    *,
    timeout: float = 60.0,
    interval: float = 1.5,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return health(url)
        except RuntimeError as exc:
            last = exc
            time.sleep(interval)
    raise RuntimeError(f"engine not healthy within {timeout}s: {last}")


def _post_json(url: str, payload: dict[str, Any], *, timeout: float = 120.0) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Content-Length": str(len(data))},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body) if body else {"ok": True}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"POST {url} failed HTTP {exc.code}: {detail[:500]}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise RuntimeError(f"POST {url} failed: {exc}") from exc


def _get_json(url: str, *, timeout: float = 60.0) -> dict[str, Any]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body) if body else {"ok": True}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET {url} failed HTTP {exc.code}: {detail[:500]}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise RuntimeError(f"GET {url} failed: {exc}") from exc


def sync_episode(
    workspace: Path,
    *,
    url: str | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    from autorefactor.sync_client import sync_workspace_http

    base = engine_base(url)
    return sync_workspace_http(
        workspace,
        url=f"{base}/v1/workspace/sync",
        label=label or workspace.name,
        source="ar_eval",
    )


def start_analyze(
    *,
    url: str | None = None,
    background: bool = True,
    skip_llm: bool = False,
) -> dict[str, Any]:
    """Start analyze on the engine active workspace (after sync)."""
    base = engine_base(url)
    return _post_json(
        f"{base}/v1/analyze",
        {"background": background, "skip_llm": skip_llm},
        timeout=120.0,
    )


def wait_analyze_job(
    job_id: str,
    *,
    url: str | None = None,
    timeout: float = 3600.0,
    interval: float = 5.0,
    on_progress: AnalyzeProgress | None = None,
) -> dict[str, Any]:
    """Poll analyze job until done/error/timeout."""
    base = engine_base(url)
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = _get_json(f"{base}/v1/analyze/jobs/{job_id}", timeout=60.0)
        if on_progress is not None:
            on_progress(last)
        status = str(last.get("status") or last.get("state") or "").lower()
        if status in {"done", "completed", "success", "finished", "error", "failed", "cancelled"}:
            return last
        if last.get("ok") is True and last.get("done") is True:
            return last
        if last.get("done") or last.get("finished"):
            return last
        time.sleep(interval)
    raise RuntimeError(f"analyze job {job_id} timed out after {timeout}s: {last}")


def analyze_active_workspace(
    *,
    url: str | None = None,
    skip_llm: bool = False,
    timeout: float = 3600.0,
    on_progress: AnalyzeProgress | None = None,
) -> dict[str, Any]:
    """Background analyze + wait. Returns final job payload."""
    started = start_analyze(url=url, background=True, skip_llm=skip_llm)
    job_id = started.get("job_id") or started.get("id")
    if not job_id:
        return started
    done = wait_analyze_job(
        str(job_id), url=url, timeout=timeout, on_progress=on_progress
    )
    return {"started": started, "job": done, "job_id": job_id}


def analyze_run_slug(analyze_result: dict[str, Any] | None) -> str | None:
    """Pin probe/status to the run this analyze wrote (not a substring match)."""
    if not isinstance(analyze_result, dict):
        return None
    job = analyze_result.get("job") if isinstance(analyze_result.get("job"), dict) else {}
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    for candidate in (result.get("slug"), job.get("slug"), analyze_result.get("slug")):
        if candidate:
            return str(candidate)
    for pathish in (result.get("out_dir"), job.get("out"), analyze_result.get("out_dir")):
        if pathish:
            name = Path(str(pathish)).name
            if name:
                return name
    return None


def fetch_changesets_for_slug(
    *,
    url: str | None = None,
    slug: str,
) -> tuple[str, list[dict[str, Any]]]:
    """GET /v1/runs/{slug}/changesets — exact slug, no label substring fallback."""
    base = engine_base(url)
    payload = _get_json(f"{base}/v1/runs/{slug}/changesets", timeout=60.0)
    items = payload.get("changesets") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return slug, []
    return slug, [c for c in items if isinstance(c, dict)]


def fetch_latest_changesets(
    *,
    url: str | None = None,
    label_substr: str | None = None,
) -> tuple[str | None, list[dict[str, Any]]]:
    """
    Return ``(slug, changesets)`` for the newest run (optionally matching label).

    Used by post-rebuild Janus probes — independent of LLM retest scoring.
    """
    base = engine_base(url)
    listing = _get_json(f"{base}/v1/runs", timeout=60.0)
    runs = listing.get("runs") if isinstance(listing, dict) else None
    if not isinstance(runs, list) or not runs:
        return None, []
    chosen: dict[str, Any] | None = None
    for row in runs:
        if not isinstance(row, dict) or not row.get("slug"):
            continue
        if label_substr:
            blob = f"{row.get('label') or ''} {row.get('display_name') or ''} {row.get('slug')}"
            if label_substr not in blob:
                continue
        chosen = row
        break
    if chosen is None:
        chosen = runs[0] if isinstance(runs[0], dict) else None
    if not chosen or not chosen.get("slug"):
        return None, []
    slug = str(chosen["slug"])
    payload = _get_json(f"{base}/v1/runs/{slug}/changesets", timeout=60.0)
    items = payload.get("changesets") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return slug, []
    return slug, [c for c in items if isinstance(c, dict)]


def restart_compose_engine(
    *,
    compose_dir: Path,
    service: str = "autorefactor",
) -> None:
    """Restart the shared engine container (no image rebuild)."""
    cmd = ["docker", "compose", "restart", service]
    subprocess.run(cmd, cwd=str(compose_dir), check=True)


def reset_for_episode(
    workspace: Path,
    *,
    engine_url: str | None = None,
    compose_dir: Path | None = None,
    restart: bool = False,
    label: str | None = None,
    sync_retries: int = 5,
    analyze: bool = False,
    analyze_skip_llm: bool = False,
    analyze_timeout: float = 3600.0,
    on_progress: AnalyzeProgress | None = None,
) -> dict[str, Any]:
    """
    Prepare the shared engine for a new episode.

    1. Health check.
    2. Optionally restart the compose service.
    3. Sync the episode tree.
    4. Optionally run analyze so a run appears in the UI.
    """
    timing: dict[str, Any] = {}
    with record_duration("initial_health_wait_s", timing):
        wait_healthy(engine_url)

    if restart and compose_dir is not None:
        with record_duration("compose_restart_s", timing):
            restart_compose_engine(compose_dir=compose_dir)
        time.sleep(2.0)
        with record_duration("restart_health_wait_s", timing):
            wait_healthy(engine_url, timeout=120.0)
    last_err: Exception | None = None
    sync: dict[str, Any] | None = None
    sync_timings: list[dict[str, Any]] = []
    for attempt in range(max(1, sync_retries)):
        try:
            attempt_t: dict[str, Any] = {"attempt": attempt}
            with record_duration("sync_episode_s", attempt_t):
                sync = sync_episode(workspace, url=engine_url, label=label)
            sync_timings.append(attempt_t)
            last_err = None
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            attempt_t: dict[str, Any] = {"attempt": attempt, "error": str(exc)}
            sync_timings.append(attempt_t)
            time.sleep(1.5 * (attempt + 1))
            try:
                wait_healthy(engine_url, timeout=30.0)
            except RuntimeError:
                pass
    if last_err is not None or sync is None:
        raise RuntimeError(f"episode sync failed after retries: {last_err}") from last_err
    analyze_result: dict[str, Any] | None = None
    if analyze:
        with record_duration("analyze_s", timing):
            analyze_result = analyze_active_workspace(
                url=engine_url,
                skip_llm=analyze_skip_llm,
                timeout=analyze_timeout,
                on_progress=on_progress,
            )
    return {
        "ok": True,
        "sync": sync,
        "restarted": bool(restart and compose_dir),
        "analyze": analyze_result,
        "timing": {
            **timing,
            "sync_attempts": sync_timings,
        },
    }
