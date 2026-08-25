from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from autorefactor.artifacts import layout_for
from autorefactor.changesets import ChangesetStatus, ChangesetStore
from autorefactor.config_store import (
    get_setup_requirements,
    load_merged_config,
    needs_setup,
    save_config,
    setup_hint,
)
from autorefactor.describe import describe_units
from autorefactor.index.store import IndexStore
from autorefactor.paths import load_run_identity, resolve_out_dir
from autorefactor.snippets import enrich_targets, snippet_blocks_for_digest
from autorefactor.stages.analyze import build_provider, run_analyze
from autorefactor.workspace_sync import (
    clone_git_workspace,
    clone_git_workspaces,
    resolve_analyze_workspace,
    resolve_workspace_info,
    workspace_status,
)


def _maybe_proxy(name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    from autorefactor.mcp.remote import proxy_tool, remote_enabled

    if not remote_enabled():
        return None
    result = proxy_tool(name, arguments)
    return result if isinstance(result, dict) else {"ok": True, "result": result}


def _require_workspace(workspace: str | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return (resolve_workspace_info, None) or (None, error_payload). Fail closed."""
    try:
        info = resolve_workspace_info(workspace)
        return info, None
    except RuntimeError as exc:
        return None, {
            "ok": False,
            "error": "workspace_unresolved",
            "detail": str(exc),
            **workspace_status(),
        }


def _workspace_echo(info: dict[str, Any]) -> dict[str, Any]:
    return {
        "workspace": info["workspace"],
        "resolved_via": info["resolved_via"],
        "sync_source": info.get("sync_source"),
        "sync_label": info.get("sync_label"),
    }


def tool_get_setup_requirements(workspace: str | None = None) -> dict[str, Any]:
    proxied = _maybe_proxy("get_setup_requirements", {"workspace": workspace})
    if proxied is not None:
        return proxied
    try:
        ws = resolve_analyze_workspace(workspace) if workspace else None
    except RuntimeError as exc:
        return {"needs_setup": True, "requirements": [{"field": "workspace", "question": str(exc)}]}
    config = load_merged_config(workspace=ws)
    reqs = get_setup_requirements(config)
    return {"needs_setup": bool(reqs), "requirements": reqs}


def tool_workspace_status() -> dict[str, Any]:
    proxied = _maybe_proxy("workspace_status", {})
    if proxied is not None:
        return proxied
    return workspace_status()


def tool_sync_workspace(path: str | None = None) -> dict[str, Any]:
    """Upload a local project tree to the remote active workspace (tar.gz over HTTP)."""
    from autorefactor.mcp.remote import (
        SyncRootError,
        ensure_remote_workspace,
        remote_enabled,
    )

    if remote_enabled():
        try:
            result = ensure_remote_workspace(path=path, force=True)
            result.setdefault("ok", True)
            result["synced"] = not result.get("skipped")
            result["next_step"] = (
                "Call analyze(path=<same>, background=true) or analyze() after this sync."
            )
            return result
        except SyncRootError as exc:
            return {
                "ok": False,
                "error": "sync_path_required",
                "detail": str(exc),
                "next_step": (
                    "Pass path='/absolute/path/to/project' or set AUTOREFACTOR_MCP_WORKSPACE. "
                    "CLI equivalent: autorefactor sync -w /path/to/project"
                ),
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "sync_failed", "detail": str(exc)[:500]}
    return {
        "ok": False,
        "error": "sync_unavailable",
        "detail": (
            "sync_workspace requires stdio MCP with AUTOREFACTOR_REMOTE_URL, "
            "or use CLI: autorefactor sync -w /path/to/project"
        ),
    }


def tool_prepare_workspace(
    git_url: str | None = None,
    ref: str | None = None,
    git_urls: list[str] | None = None,
) -> dict[str, Any]:
    proxied = _maybe_proxy(
        "prepare_workspace",
        {"git_url": git_url, "ref": ref, "git_urls": git_urls},
    )
    if proxied is not None:
        return proxied
    try:
        urls = [u for u in (git_urls or []) if (u or "").strip()]
        if git_url and str(git_url).strip():
            urls = [str(git_url).strip(), *urls]
        # de-dupe preserve order
        seen: set[str] = set()
        ordered: list[str] = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                ordered.append(u)
        if not ordered:
            return {
                "ok": False,
                "error": "prepare_failed",
                "detail": (
                    "Provide git_url or git_urls. "
                    "For a local open project use analyze (auto-sync) or sync_workspace instead."
                ),
            }
        if len(ordered) == 1:
            result = clone_git_workspace(ordered[0], ref=ref)
        else:
            result = clone_git_workspaces(ordered, ref=ref)
        result["next_step"] = (
            "Advanced only: remote git clone. For local projects call analyze "
            "(cwd syncs automatically) or sync_workspace — do not prepare_workspace. "
            "If you must use this path, call analyze with workspace=active"
            + (" (background=true recommended for multi-repo)" if len(ordered) > 1 else "")
            + "."
        )
        return result
    except RuntimeError as exc:
        return {
            "ok": False,
            "error": "prepare_failed",
            "detail": str(exc),
            "hint": (
                "Local projects: use analyze or sync_workspace. "
                "Private git: set AUTOREFACTOR_GIT_TOKEN on the server; never put tokens in git_url."
            ),
        }


def tool_save_config(partial: dict[str, Any], user_level: bool = False) -> dict[str, Any]:
    proxied = _maybe_proxy("save_config", {"partial": partial, "user_level": user_level})
    if proxied is not None:
        return proxied
    path = save_config(partial, user_level=user_level)
    return {"ok": True, "path": str(path)}


def _analyze_sync(
    workspace: str | None,
    out: str | None,
    skip_llm: bool,
    describe_mode: str | None,
    focus: str | None,
    progress=None,  # noqa: ANN001
) -> dict[str, Any]:
    try:
        ws = resolve_analyze_workspace(workspace)
    except RuntimeError as exc:
        return {"ok": False, "error": "no_workspace", "detail": str(exc), **workspace_status()}
    out_dir = resolve_out_dir(ws, Path(out) if out else None)
    config = load_merged_config(
        workspace=ws,
        out_dir=out_dir,
        cli_overrides={
            "skip_llm": skip_llm,
            **({"describe_mode": describe_mode} if describe_mode else {}),
            **({"focus": focus} if focus else {}),
        },
    )
    config.skip_llm = skip_llm or config.skip_llm
    if describe_mode:
        config.describe_mode = describe_mode
    if focus:
        config.focus = focus
    if needs_setup(config) and not skip_llm:
        return {
            "ok": False,
            "error": "needs_setup",
            **tool_get_setup_requirements(str(ws)),
        }
    artifacts = run_analyze(config, progress=progress)
    layout = layout_for(config.resolve_out_dir())
    metrics_path = layout.out_dir / "report" / "metrics.json"
    impact: dict[str, Any] = {}
    if metrics_path.exists():
        try:
            payload = json.loads(metrics_path.read_text(encoding="utf-8"))
            impact = payload.get("impact") or {}
        except (OSError, json.JSONDecodeError):
            impact = {}
    touched = (
        list(artifacts.document_report.touched_paths)
        if artifacts.document_report is not None
        else []
    )
    from autorefactor.enrich_queue import pending_enrich_count

    enrich_pending = pending_enrich_count(layout.out_dir)
    runtime = config.runtime_summary()
    if enrich_pending > 0:
        next_step = (
            f"Drain {enrich_pending} enrich task(s) via get_next_enrich_task / "
            "submit_enrich_result, then get_report and get_next_changeset. "
            "(Workflow is in MCP instructions; get_agent_playbook for live backend detail.)"
        )
    else:
        next_step = (
            "Call get_report and get_next_changeset to start the agent queue. "
            "(Workflow is in MCP instructions; get_agent_playbook for live backend detail.)"
        )
    return {
        "ok": True,
        "workspace": str(ws),
        "confirmed": artifacts.manifest.confirmed_count if artifacts.manifest else 0,
        "flags": artifacts.manifest.flag_count if artifacts.manifest else 0,
        "out_dir": str(layout.out_dir),
        "slug": layout.out_dir.name,
        "analyze_dir": str(layout.analyze_dir),
        "report_md": str(layout.out_dir / "report" / "report.md"),
        "queue": str(layout.out_dir / "queue" / "changesets.json"),
        "queue_compat": str(layout.out_dir / "changesets" / "queue.json"),
        "enrich_queue": str(layout.out_dir / "queue" / "enrich_tasks.json"),
        "enrich_pending": enrich_pending,
        "focus": config.resolved_focus(),
        "describe_mode": config.resolved_describe_mode(),
        "chat_backend": runtime["chat_backend"],
        "embed_backend": runtime["embed_backend"],
        "docs_written": len(touched),
        "touched_paths": touched,
        "headline": impact.get("headline"),
        "planner_coverage_percent": impact.get("planner_coverage_percent"),
        "cross_repo_opportunities": impact.get("cross_repo_opportunities"),
        "loc_under_review": impact.get("loc_under_review"),
        "agent_ready_changesets": impact.get("agent_ready_changesets"),
        "next_step": next_step,
    }


def tool_analyze(
    workspace: str | None = None,
    out: str | None = None,
    skip_llm: bool = False,
    describe_mode: str | None = None,
    focus: str | None = None,
    background: bool = True,
    path: str | None = None,
) -> dict[str, Any]:
    proxied = _maybe_proxy(
        "analyze",
        {
            "workspace": workspace,
            "out": out,
            "skip_llm": skip_llm,
            "describe_mode": describe_mode,
            "focus": focus,
            "background": background,
            "path": path,
        },
    )
    if proxied is not None:
        return proxied
    try:
        ws = resolve_analyze_workspace(workspace)
    except RuntimeError as exc:
        return {"ok": False, "error": "no_workspace", "detail": str(exc), **workspace_status()}
    info = resolve_workspace_info(workspace)
    out_dir = resolve_out_dir(ws, Path(out) if out else None)
    lock_key = str(out_dir.resolve())

    from autorefactor import jobs as jobs_mod

    if background:
        started = jobs_mod.start_analyze_job(
            workspace=workspace,
            out=out,
            skip_llm=skip_llm,
            describe_mode=describe_mode,
            focus=focus,
            lock_key=lock_key,
            runner=_analyze_sync,
        )
        started.update(_workspace_echo(info))
        return started
    active = jobs_mod.active_job_id(lock_key)
    if active:
        return {
            "ok": False,
            "error": "analyze_already_running",
            "job_id": active,
            "detail": "A background analyze is already running for this out_dir; poll get_progress or get_analyze_job.",
            "poll": "get_progress",
            **_workspace_echo(info),
        }
    result = _analyze_sync(workspace, out, skip_llm, describe_mode, focus)
    if isinstance(result, dict):
        result.update(_workspace_echo(info))
    return result


def tool_get_analyze_job(job_id: str) -> dict[str, Any]:
    proxied = _maybe_proxy("get_analyze_job", {"job_id": job_id})
    if proxied is not None:
        return proxied
    return _local_get_progress(job_id=job_id)


def tool_list_jobs(limit: int = 20, status: str | None = None) -> dict[str, Any]:
    proxied = _maybe_proxy("list_jobs", {"limit": limit, "status": status})
    if proxied is not None:
        return proxied
    from autorefactor.jobs import list_jobs

    jobs = list_jobs(limit=limit, status=status)
    return {"ok": True, "jobs": jobs, "count": len(jobs)}


def _local_get_progress(
    job_id: str | None = None,
    workspace: str | None = None,
    out: str | None = None,
) -> dict[str, Any]:
    from autorefactor.jobs import resolve_job

    if job_id:
        return resolve_job(job_id=job_id)

    lock_key: str | None = None
    try:
        ws = resolve_analyze_workspace(workspace)
        out_dir = resolve_out_dir(ws, Path(out) if out else None)
        lock_key = str(out_dir.resolve())
    except RuntimeError:
        lock_key = None

    if lock_key:
        return resolve_job(lock_key=lock_key)
    return resolve_job()


def tool_get_progress(
    job_id: str | None = None,
    workspace: str | None = None,
    out: str | None = None,
) -> dict[str, Any]:
    proxied = _maybe_proxy(
        "get_progress",
        {"job_id": job_id, "workspace": workspace, "out": out},
    )
    if proxied is not None:
        return proxied
    return _local_get_progress(job_id=job_id, workspace=workspace, out=out)


def _queue_fingerprint_stale_count(items: list) -> int:  # noqa: ANN001
    return sum(1 for c in items if (c.metrics_delta or {}).get("fingerprint_stale"))


def _status_counts(items: list) -> dict[str, int]:  # noqa: ANN001
    by_status: dict[str, int] = {}
    for item in items:
        key = item.status.value
        by_status[key] = by_status.get(key, 0) + 1
    return by_status


def _freshness_queue_snapshot(
    ws: Path,
    out_dir: Path,
) -> tuple[dict[str, Any], list, dict[str, int], int]:
    """Run freshness (may invalidate queue), then load a single post-invalidation snapshot."""
    from autorefactor.freshness import freshness_payload
    from autorefactor.queue import load_queue

    fresh = freshness_payload(workspace=ws, out_dir=out_dir if out_dir.is_dir() else None)
    items = load_queue(out_dir)
    return fresh, items, _status_counts(items), _queue_fingerprint_stale_count(items)


def _queue_freshness_gate(
    *,
    workspace: Path,
    out_dir: Path,
    active_job: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Block queue/report when run fingerprint is missing or stale."""
    from autorefactor.freshness import freshness_payload

    fresh = freshness_payload(workspace=workspace, out_dir=out_dir if out_dir.is_dir() else None)
    if fresh.get("fresh"):
        return None
    if fresh.get("apply_safe"):
        return None
    reason = str((fresh.get("freshness") or {}).get("reason") or "")
    if active_job:
        return {
            "ok": False,
            "queue_blocked": True,
            "fresh": False,
            "freshness": fresh.get("freshness"),
            "active_job": active_job,
            "error": "analyze_in_progress",
            "detail": "Analyze is running; queue unlocks when the job completes.",
            "next_step": (
                "Poll get_progress or get_status until active_job finishes, then call "
                "get_next_changeset."
            ),
        }
    if reason == "missing_fingerprint":
        next_step = (
            "Queue blocked (missing_fingerprint). Call analyze(background=true) and poll "
            "get_progress / get_status until fresh=true before get_next_changeset."
        )
    elif reason == "content_changed":
        next_step = (
            "Workspace changed since last analyze (content_changed). Call analyze(background=true) "
            "and poll get_progress / get_status until fresh=true."
        )
    else:
        next_step = (
            "Run analyze(background=true) and poll get_progress / get_status until fresh=true "
            "before using the queue."
        )
    return {
        "ok": False,
        "queue_blocked": True,
        "fresh": False,
        "freshness": fresh.get("freshness"),
        "error": "stale_run",
        "detail": f"Run is not fresh ({reason or 'stale'}).",
        "next_step": next_step,
    }


def tool_get_status(workspace: str | None = None, out: str | None = None) -> dict[str, Any]:
    proxied = _maybe_proxy("get_status", {"workspace": workspace, "out": out})
    if proxied is not None:
        return proxied
    from autorefactor.jobs import active_job_id, job_status

    info, err = _require_workspace(workspace)
    if err is not None:
        return err
    assert info is not None
    ws = info["path"]
    out_dir = resolve_out_dir(ws, Path(out) if out else None)
    config = load_merged_config(workspace=ws, out_dir=out_dir)
    report_md = out_dir / "report" / "report.md"
    lock_key = str(out_dir.resolve())
    active = active_job_id(lock_key)
    from autorefactor.enrich_queue import pending_enrich_count
    from autorefactor.enrich_queue.store import EnrichTaskStore, enrich_queue_path
    from autorefactor.queue import load_queue

    if active:
        # Full workspace fingerprint scans block HTTP/MCP for minutes on large repos
        # while analyze already holds the GIL — skip until the job finishes.
        items = load_queue(out_dir)
        by_status = _status_counts(items)
        fingerprint_stale = _queue_fingerprint_stale_count(items)
        fresh = {
            "fresh": False,
            "apply_safe": False,
            "freshness": {
                "reason": "analyze_in_progress",
                "run_fingerprint": None,
                "current_fingerprint": None,
                "apply_safe": False,
            },
        }
    else:
        fresh, items, by_status, fingerprint_stale = _freshness_queue_snapshot(ws, out_dir)

    enrich_items = EnrichTaskStore(enrich_queue_path(out_dir)).load()
    enrich_by: dict[str, int] = {}
    for item in enrich_items:
        key = item.status.value
        enrich_by[key] = enrich_by.get(key, 0) + 1
    runtime = config.runtime_summary()
    metrics_path = out_dir / "report" / "metrics.json"
    pipeline: dict[str, Any] = {}
    if metrics_path.is_file():
        try:
            pipeline = (json.loads(metrics_path.read_text(encoding="utf-8")) or {}).get(
                "pipeline"
            ) or {}
        except (json.JSONDecodeError, OSError):
            pipeline = {}
    payload: dict[str, Any] = {
        "ok": True,
        **_workspace_echo(info),
        "out_dir": str(out_dir),
        "needs_setup": needs_setup(config),
        "setup_hint": setup_hint(config),
        "describe_mode": config.resolved_describe_mode(),
        "focus": config.resolved_focus(),
        "chat_backend": runtime["chat_backend"],
        "embed_backend": runtime["embed_backend"],
        "parallel_llm": runtime["parallel_llm"],
        "embed_batch_size": runtime.get("embed_batch_size"),
        "embed_concurrency": runtime.get("embed_concurrency"),
        "effective_embed_parallel": runtime.get("effective_embed_parallel"),
        "pipeline": pipeline,
        "changesets": len(items),
        "by_status": by_status,
        "enrich_tasks": len(enrich_items),
        "enrich_pending": pending_enrich_count(out_dir),
        "enrich_by_status": enrich_by,
        "report": report_md.exists(),
        "report_md": str(report_md) if report_md.exists() else None,
        "active_job": None,
    }
    if active:
        payload["active_job"] = job_status(active)
    payload.update(fresh)
    payload["fingerprint_stale"] = fingerprint_stale
    reason = str((fresh.get("freshness") or {}).get("reason") or "")
    if not fresh.get("fresh"):
        cta = fresh.get("re_analyze_cta")
        if cta:
            payload["stale_warning"] = cta
            if not payload.get("active_job"):
                payload.setdefault("next_step", cta)
    if not fresh.get("fresh") and reason == "missing_fingerprint" and not payload.get("active_job"):
        payload["ok"] = False
        payload["queue_blocked"] = True
        payload["next_step"] = (
            "Queue blocked (missing_fingerprint). Call analyze(background=true) and poll "
            "get_progress / get_status until fresh=true before get_next_changeset."
        )
    threshold = float(config.confidence_threshold)
    payload["confidence_threshold"] = threshold
    counts = _queue_counts(out_dir, threshold=threshold, items=items)
    payload["queue_counts"] = counts
    payload["hitl_burden"] = _hitl_burden(counts)
    return payload


def _stale_report_banner(fresh: dict[str, Any]) -> str:
    reason = str((fresh.get("freshness") or {}).get("reason") or "stale")
    return (
        "**STALE RUN — report may not match the workspace "
        f"({reason}).** Re-run analyze(background=true) before applying queue items."
    )


def _prep_coverage_cta(out_dir: Path) -> dict[str, Any] | None:
    """Single prep-phase coverage question — not per ensure_tests item."""
    from autorefactor.changesets import ChangesetStatus
    from autorefactor.queue import load_queue

    items = load_queue(out_dir)
    ensure = [
        c
        for c in items
        if (c.action or "").strip().lower() == "ensure_tests"
        and c.status in {ChangesetStatus.PENDING, ChangesetStatus.IN_PROGRESS}
    ]
    if not ensure:
        return None
    targets = sum(len(c.targets or []) for c in ensure)
    batched = any((c.metrics_delta or {}).get("batched_ensure_tests_prep") for c in ensure)
    return {
        "ensure_tests_count": len(ensure),
        "target_units": targets,
        "batched": batched,
        "question": (
            "You are about to refactor without full test coverage. Push coverage to the limit "
            "on all flagged production code before any structural refactors?"
        ),
        "reply_format": "yes or no",
        "if_yes": "Drain ensure_tests with auto_proceed; never ask per class.",
        "if_no": "confirm_changeset(skip) on each ensure_tests, then structural refactors.",
    }


def tool_get_report(
    out: str | None = None,
    workspace: str | None = None,
    allow_stale: bool = False,
) -> dict[str, Any]:
    proxied = _maybe_proxy(
        "get_report",
        {"out": out, "workspace": workspace, "allow_stale": allow_stale},
    )
    if proxied is not None:
        return proxied
    info, err = _require_workspace(workspace)
    if err is not None:
        return err
    assert info is not None
    ws = info["path"]
    out_dir = resolve_out_dir(ws, Path(out) if out else None)
    report_md = out_dir / "report" / "report.md"
    metrics = out_dir / "report" / "metrics.json"
    fresh, _items, _by_status, fingerprint_stale = _freshness_queue_snapshot(ws, out_dir)
    base: dict[str, Any] = {
        **_workspace_echo(info),
        "out_dir": str(out_dir),
        "report_md": str(report_md) if report_md.exists() else None,
        "metrics_json": str(metrics) if metrics.exists() else None,
        "visuals_dir": str(out_dir / "report" / "visuals"),
    }
    base.update(fresh)
    base["fingerprint_stale"] = fingerprint_stale
    is_stale = not fresh.get("fresh") and not fresh.get("apply_safe")
    payload = dict(base)
    payload["ok"] = True
    if is_stale:
        payload["stale"] = True
        payload["stale_banner"] = _stale_report_banner(fresh)
        payload["next_step"] = (
            "Report is stale — metrics reflect the last analyze. "
            "Call analyze(background=true) before applying refactors."
        )
    elif not fresh.get("fresh"):
        payload["stale"] = True
    if allow_stale and is_stale:
        payload["allow_stale"] = True
    if metrics.exists():
        payload["metrics"] = json.loads(metrics.read_text(encoding="utf-8"))
        impact = payload["metrics"].get("impact") or {}
        if impact:
            payload["impact"] = impact
            payload["headline"] = impact.get("headline")
            payload["pitch"] = impact.get("pitch")
            payload["upside_bullets"] = impact.get("upside_bullets") or []
            payload["why_refactor"] = {
                "pitch": impact.get("pitch"),
                "bullets": impact.get("upside_bullets") or [],
                "estimated_loc_removed": impact.get("estimated_loc_removed"),
                "loc_under_review": impact.get("loc_under_review"),
                "cross_repo_opportunities": impact.get("cross_repo_opportunities"),
                "high_confidence_changesets": impact.get("high_confidence_changesets"),
            }
    if report_md.exists():
        # First ~80 lines are the upside narrative; keep MCP payloads small.
        text = report_md.read_text(encoding="utf-8")
        payload["report_excerpt"] = "\n".join(text.splitlines()[:80])
    if is_stale:
        banner = payload.get("stale_banner") or _stale_report_banner(fresh)
        excerpt = payload.get("report_excerpt") or ""
        payload["report_excerpt"] = f"{banner}\n\n{excerpt}".strip()
    prep = _prep_coverage_cta(out_dir)
    if prep is not None:
        payload["prep_coverage_cta"] = prep
    if not is_stale:
        slug = out_dir.name
        queue_url = f"http://localhost:3000/runs/{slug}/queue"
        bits = [
            f"Queue UI: {queue_url} — Approve/Skip HITL items there, then tell me to execute the approved queue.",
        ]
        if prep is not None:
            bits.append(
                "Before structural walk: answer prep coverage yes/no "
                f"({prep.get('ensure_tests_count', 0)} ensure_tests pending)."
            )
        else:
            bits.append("No ensure_tests prep pending — ready for queue walk when you say so.")
        existing = (payload.get("next_step") or "").strip()
        payload["next_step"] = f"{' '.join(bits)}" + (f" {existing}" if existing else "")
        payload["queue_url"] = queue_url
        payload["execute_cta"] = (
            "After Approve/Skip in the queue UI, tell the agent to execute the approved items."
        )
    return payload


def _queue_store(out_dir: Path) -> ChangesetStore:
    from autorefactor.queue import load_queue

    v2 = out_dir / "queue" / "changesets.json"
    compat = out_dir / "changesets" / "queue.json"
    path = v2 if v2.exists() else compat
    # Ensure store sees latest even if only one path was written
    if v2.exists() or compat.exists():
        return ChangesetStore(path)
    # Prefer v2 path for new empty queues
    return ChangesetStore(v2)


def _queue_counts(
    out_dir: Path,
    *,
    threshold: float,
    items: list | None = None,  # noqa: ANN001
) -> dict[str, int]:
    from autorefactor.changesets.store import changeset_needs_hitl
    from autorefactor.queue import load_queue

    if items is None:
        items = load_queue(out_dir)
    done = skipped = blocked = pending = 0
    pending_autorun = pending_hitl = pending_waiting = 0
    ids_done = {c.id for c in items if c.status == ChangesetStatus.DONE}
    for c in items:
        st = c.status
        if st == ChangesetStatus.DONE:
            done += 1
        elif st == ChangesetStatus.SKIPPED:
            skipped += 1
        elif st == ChangesetStatus.BLOCKED:
            blocked += 1
        elif st in {ChangesetStatus.PENDING, ChangesetStatus.IN_PROGRESS}:
            pending += 1
            deps_ok = all(dep in ids_done for dep in (c.depends_on or []))
            needs_hitl, _reason = changeset_needs_hitl(
                c, threshold, deps_satisfied=deps_ok, queue=items
            )
            if not deps_ok:
                pending_waiting += 1
            elif needs_hitl:
                pending_hitl += 1
            else:
                pending_autorun += 1
    return {
        "pending": pending,
        "pending_autorun": pending_autorun,
        "pending_hitl": pending_hitl,
        "pending_waiting": pending_waiting,
        "done": done,
        "skipped": skipped,
        "blocked": blocked,
    }


def _hitl_burden(counts: dict[str, int]) -> dict[str, float | int]:
    pending = int(counts.get("pending") or 0)
    pending_hitl = int(counts.get("pending_hitl") or 0)
    pending_autorun = int(counts.get("pending_autorun") or 0)
    pending_waiting = int(counts.get("pending_waiting") or 0)
    return {
        "pending": pending,
        "pending_hitl": pending_hitl,
        "pending_autorun": pending_autorun,
        "pending_waiting": pending_waiting,
        "ratio": pending_hitl / max(pending, 1),
    }


def _sample_queue_item(
    cs,  # noqa: ANN001
    *,
    threshold: float,
    needs_confirmation: bool,
    hitl_reason: str | None,
    out_dir: Path,
    describe_mode: str = "raw",
) -> dict[str, Any]:
    enriched = enrich_targets(out_dir, cs.targets or [])
    digest = _hitl_digest(cs, threshold, enriched_targets=enriched)
    item: dict[str, Any] = {
        "id": cs.id,
        "title": cs.title,
        "action": cs.action,
        "confidence": cs.confidence,
        "status": cs.status.value,
        "needs_confirmation": needs_confirmation,
        "auto_proceed": (not needs_confirmation) and hitl_reason != "blocked_deps",
        "hitl_reason": hitl_reason,
        "hitl_digest": digest,
        "brief": cs.brief,
        "targets": enriched[:8],
    }
    if needs_confirmation:
        decisions, optional = _hitl_choice_groups(
            cs,
            needs_confirmation=True,
            describe_mode=describe_mode,
            hitl_reason=hitl_reason,
            threshold=threshold,
        )
        item["hitl_review"] = _hitl_review(
            cs,
            threshold=threshold,
            hitl_reason=hitl_reason,
            enriched_targets=enriched,
            decisions=decisions,
            optional=optional,
        )
    return item


def _prep_units_unresolved(out_dir: Path, cs) -> tuple[bool, list[str]]:  # noqa: ANN001
    action_l = (cs.action or "").strip().lower()
    if action_l not in {"ensure_tests", "document"}:
        return False, []
    try:
        units = _resolve_changeset_units(out_dir, cs)
    except Exception:  # noqa: BLE001
        units = []
    if units:
        return False, []
    target_paths = [
        f"{t.repo}/{t.path}" if t.repo else t.path for t in (cs.targets or [])
    ]
    return True, target_paths


def _peek_loop(out_dir: Path, *, threshold: float) -> str:
    """What the agent should do next after the current item: autorun | hitl | waiting | done."""
    result = _queue_store(out_dir).next_pending(min_confidence=threshold)
    if result.done or result.changeset is None:
        return "done"
    if result.hitl_reason == "blocked_deps":
        return "waiting"
    if result.needs_confirmation:
        return "hitl"
    return "autorun"


def _loop_payload(
    out_dir: Path,
    *,
    threshold: float,
    current_auto_proceed: bool | None = None,
    current_waiting: bool = False,
) -> dict[str, Any]:
    """Attach loop + queue_counts. current_auto_proceed forces loop for the active item."""
    counts = _queue_counts(out_dir, threshold=threshold)
    if current_waiting:
        loop = "waiting"
    elif current_auto_proceed is True:
        loop = "autorun"
    elif current_auto_proceed is False:
        loop = "hitl"
    else:
        loop = _peek_loop(out_dir, threshold=threshold)
    return {"loop": loop, "queue_counts": counts}


def tool_sample_queue(
    out: str | None = None,
    workspace: str | None = None,
    hitl_limit: int = 3,
) -> dict[str, Any]:
    """Read-only peek: HITL burden plus 1 autorun and 1–3 low-confidence HITL items."""
    proxied = _maybe_proxy(
        "sample_queue",
        {"out": out, "workspace": workspace, "hitl_limit": hitl_limit},
    )
    if proxied is not None:
        return proxied
    info, err = _require_workspace(workspace)
    if err is not None:
        return err
    assert info is not None
    ws = info["path"]
    out_dir = resolve_out_dir(ws, Path(out) if out else None)
    config = load_merged_config(workspace=ws, out_dir=out_dir)
    threshold = float(config.confidence_threshold)
    cap = max(1, min(int(hitl_limit or 3), 8))
    from autorefactor.changesets.store import changeset_needs_hitl

    fresh, items, _by_status, fingerprint_stale = _freshness_queue_snapshot(ws, out_dir)
    ids_done = {c.id for c in items if c.status == ChangesetStatus.DONE}
    pending = [
        c for c in items if c.status in {ChangesetStatus.PENDING, ChangesetStatus.IN_PROGRESS}
    ]
    auto_items: list[tuple[Any, str | None]] = []
    hitl_items: list[tuple[Any, str | None]] = []
    for c in pending:
        deps_ok = all(dep in ids_done for dep in (c.depends_on or []))
        needs_hitl, reason = changeset_needs_hitl(
            c, threshold, deps_satisfied=deps_ok, queue=items
        )
        unresolved, _paths = _prep_units_unresolved(out_dir, c)
        if unresolved:
            needs_hitl = True
            reason = reason or "units_unresolved"
        if not deps_ok:
            continue
        if needs_hitl:
            hitl_items.append((c, reason))
        else:
            auto_items.append((c, reason))
    auto_items.sort(key=lambda pair: (-float(pair[0].confidence), pair[0].id))
    hitl_items.sort(key=lambda pair: (float(pair[0].confidence), pair[0].id))

    if not pending:
        historical = [
            c
            for c in items
            if c.status in {ChangesetStatus.DONE, ChangesetStatus.SKIPPED}
        ]
        for c in historical:
            deps_ok = all(dep in ids_done for dep in (c.depends_on or []))
            needs_hitl, reason = changeset_needs_hitl(
                c, threshold, deps_satisfied=deps_ok, queue=items
            )
            unresolved, _paths = _prep_units_unresolved(out_dir, c)
            if unresolved:
                needs_hitl = True
                reason = reason or "units_unresolved"
            if c.status == ChangesetStatus.SKIPPED or needs_hitl:
                hitl_items.append((c, reason))
            elif c.status == ChangesetStatus.DONE and not needs_hitl:
                auto_items.append((c, reason))
        auto_items.sort(key=lambda pair: (-float(pair[0].confidence), pair[0].id))
        hitl_items.sort(key=lambda pair: (float(pair[0].confidence), pair[0].id))

    describe_mode = (
        config.resolved_describe_mode()
        if hasattr(config, "resolved_describe_mode")
        else getattr(config, "describe_mode", "raw")
    )
    counts = _queue_counts(out_dir, threshold=threshold, items=items)
    auto_sample = [
        _sample_queue_item(
            c,
            threshold=threshold,
            needs_confirmation=False,
            hitl_reason=reason,
            out_dir=out_dir,
            describe_mode=describe_mode,
        )
        for c, reason in auto_items[:1]
    ]
    hitl_sample = [
        _sample_queue_item(
            c,
            threshold=threshold,
            needs_confirmation=True,
            hitl_reason=reason,
            out_dir=out_dir,
            describe_mode=describe_mode,
        )
        for c, reason in hitl_items[:cap]
    ]
    next_step = (
        "Inspect auto_sample[0] and hitl_sample briefs. "
        "Use get_changeset for full detail. Do not confirm or mark."
    )
    if not fresh.get("fresh"):
        cta = fresh.get("re_analyze_cta") or (
            "Run is stale — call analyze(background=true) before queue walk."
        )
        next_step = f"{cta} {next_step}"
    return {
        "ok": True,
        **_workspace_echo(info),
        "out_dir": str(out_dir),
        "confidence_threshold": threshold,
        "allow_stale": False,
        "fresh": fresh.get("fresh"),
        "apply_safe": fresh.get("apply_safe"),
        "metrics_review_ok": fresh.get("metrics_review_ok"),
        "re_analyze_cta": fresh.get("re_analyze_cta"),
        "stale_queue_invalidated": fresh.get("stale_queue_invalidated"),
        "fingerprint_stale": fingerprint_stale,
        "stale_warning": fresh.get("re_analyze_cta") if not fresh.get("fresh") else None,
        "queue_counts": counts,
        "hitl_burden": _hitl_burden(counts),
        "auto_sample": auto_sample,
        "hitl_sample": hitl_sample,
        "next_step": next_step,
    }


def tool_list_changesets(out: str | None = None, workspace: str | None = None) -> dict[str, Any]:
    proxied = _maybe_proxy("list_changesets", {"out": out, "workspace": workspace})
    if proxied is not None:
        return proxied
    try:
        ws = resolve_analyze_workspace(workspace)
    except RuntimeError:
        ws = Path.cwd()
    out_dir = resolve_out_dir(ws, Path(out) if out else None)
    from autorefactor.queue import load_queue

    items = load_queue(out_dir)
    return {
        "out_dir": str(out_dir),
        "changesets": [
            {
                "id": c.id,
                "title": c.title,
                "action": c.action,
                "confidence": c.confidence,
                "status": c.status.value,
                "parallel_group": c.parallel_group,
                "cross_repo": bool(c.metrics_delta.get("cross_repo")),
                "repos": sorted({t.repo for t in c.targets if t.repo}),
            }
            for c in items
        ]
    }


def tool_get_next_changeset(
    out: str | None = None,
    workspace: str | None = None,
    min_confidence: float | None = None,
) -> dict[str, Any]:
    proxied = _maybe_proxy(
        "get_next_changeset",
        {"out": out, "workspace": workspace, "min_confidence": min_confidence},
    )
    if proxied is not None:
        return proxied
    try:
        ws = resolve_analyze_workspace(workspace)
    except RuntimeError:
        ws = Path.cwd()
    out_dir = resolve_out_dir(ws, Path(out) if out else None)
    config = load_merged_config(workspace=ws, out_dir=out_dir)
    threshold = config.confidence_threshold if min_confidence is None else min_confidence
    from autorefactor.jobs import active_job_id, job_status

    lock_key = str(out_dir.resolve())
    active = active_job_id(lock_key)
    active_payload = job_status(active) if active else None
    blocked = _queue_freshness_gate(
        workspace=ws,
        out_dir=out_dir,
        active_job=active_payload,
    )
    if blocked is not None:
        blocked["out_dir"] = str(out_dir)
        blocked["queue_counts"] = _queue_counts(out_dir, threshold=threshold)
        return blocked
    store = _queue_store(out_dir)
    result = store.next_pending(min_confidence=threshold)
    if result.done:
        counts = _queue_counts(out_dir, threshold=threshold)
        return {
            "done": True,
            "out_dir": str(out_dir),
            "loop": "done",
            "queue_counts": counts,
            "next_step": (
                "Changeset queue empty. Before claiming done: run the project test suite "
                "once (mvn test / pytest / npm test) and report results."
            ),
        }
    assert result.changeset is not None
    cs = result.changeset
    needs_confirmation = bool(result.needs_confirmation)
    unresolved, target_paths = _prep_units_unresolved(out_dir, cs)
    if unresolved:
        needs_confirmation = True
    hitl_reason = result.hitl_reason
    waiting = hitl_reason == "blocked_deps" and not unresolved
    if waiting:
        needs_confirmation = False
    if unresolved and not hitl_reason:
        hitl_reason = "units_unresolved"
    ui_approved = (cs.user_decision or "").strip().lower() == "proceed"
    if ui_approved and not waiting and not unresolved:
        needs_confirmation = False
        hitl_reason = None
    auto_proceed = (not needs_confirmation) and not waiting
    # Autorun must stay lean — full MAX_SOURCE_CHARS snippets in digests time out MCP
    # on large multi-repo queues. Reserve full enrich for HITL confirmation only.
    if needs_confirmation or waiting:
        enriched_targets = enrich_targets(out_dir, cs.targets or [])
        digest = _hitl_digest(cs, threshold, enriched_targets=enriched_targets)
    else:
        enriched_targets = [
            {
                "repo": getattr(t, "repo", None) if not isinstance(t, dict) else t.get("repo"),
                "path": getattr(t, "path", None) if not isinstance(t, dict) else t.get("path"),
                "symbol": getattr(t, "symbol", None) if not isinstance(t, dict) else t.get("symbol"),
                "unit_id": getattr(t, "unit_id", None) if not isinstance(t, dict) else t.get("unit_id"),
                "qualified_name": getattr(t, "qualified_name", None)
                if not isinstance(t, dict)
                else t.get("qualified_name"),
                "source_snippet": "",
            }
            for t in (cs.targets or [])
        ]
        digest = _hitl_digest(cs, threshold, enriched_targets=enriched_targets)
    if unresolved:
        paths_note = ", ".join(f"`{p}`" for p in target_paths) or "(no targets)"
        digest += f"\nunits_unresolved: no index units match targets {paths_note}"
    describe_mode = (
        config.resolved_describe_mode()
        if hasattr(config, "resolved_describe_mode")
        else getattr(config, "describe_mode", "raw")
    )
    decisions, optional = _hitl_choice_groups(
        cs,
        needs_confirmation=needs_confirmation,
        describe_mode=describe_mode,
        hitl_reason=hitl_reason,
        threshold=threshold,
    )
    questions: list[str] = []
    if needs_confirmation or waiting:
        questions = list(result.questions) if result.questions else [
            f"Confidence {cs.confidence:.2f} is below threshold {threshold:.2f}.",
            "Proceed with this changeset, skip it, or mark blocked?",
        ]
        questions.insert(0, digest)
    action_l = (cs.action or "").strip().lower()
    if waiting:
        next_step = (
            "loop=waiting: dependencies are not done. Do not confirm_changeset(proceed) "
            "and do not ask the user to confirm this item. Drain or skip the blocking "
            "prep changeset first, then call get_next_changeset again."
        )
    elif auto_proceed and ui_approved:
        next_step = (
            "loop=autorun: UI already approved (user_decision=proceed). Do not ask again. "
            "Call confirm_changeset(decision=proceed), apply the refactor, mark_changeset done, "
            "then immediately call get_next_changeset. Drain the entire approved/autorun streak."
        )
    elif auto_proceed:
        next_step = (
            "loop=autorun: Do not ask the user. Call confirm_changeset(decision=proceed) "
            "without asking, apply the refactor, mark_changeset done, then immediately "
            "call get_next_changeset. Drain the entire autorun streak before stopping. "
            "Do not apply optional_choices unless the user already opted in."
        )
    elif action_l == "document":
        next_step = (
            "loop=hitl prep/document: Present hitl_review with code snippets; ask proceed/skip/blocked. "
            "Proceed = write in-source docs for targets"
            + (
                " (server will apply generated descriptions when describe_mode=describe)"
                if describe_mode == "describe"
                else " (agent writes docs; describe_mode=raw has no generated text to apply)"
            )
            + ". Do not offer a separate write_docs optional — that is this item. "
            "Or point the user to Approve/Skip in the queue UI, then execute."
        )
    elif action_l == "ensure_tests":
        next_step = (
            "loop=autorun prep/ensure_tests: Do NOT ask the user — prep coverage yes/no already "
            "decided. Proceed means write or expand characterization tests for listed targets "
            "until Jacoco-relevant behavior is covered (test file exists ≠ done). "
            "Call confirm_changeset(decision=proceed) if prep was yes, skip if prep was no, "
            "then mark_changeset and get_next_changeset. Never show per-class proceed/skip/blocked."
        )
    elif hitl_reason == "architectural_wiring":
        next_step = (
            "loop=hitl architectural_wiring: Present hitl_review with code snippets; ask wire_kafka_consumer vs "
            "remove_dead_wiring (or skip/blocked). Do not auto-apply extract_shared on "
            "test-only handlers. Or finish Approve/Skip in the queue UI, then execute."
        )
    elif hitl_reason == "security_sensitive":
        next_step = (
            "loop=hitl security_sensitive: Present hitl_review with code snippets; never auto_proceed on committed "
            "secrets/credentials. Ask proceed/skip/blocked; rotate secrets and externalize "
            "via env/vault if proceeding. Or finish Approve/Skip in the queue UI, then execute."
        )
    elif hitl_reason == "plan_review":
        next_step = (
            "loop=hitl plan_review: Planner marked this for human review. Present hitl_review "
            "with rationale and code snippets; ask proceed/skip/blocked. "
            "Or finish Approve/Skip in the queue UI, then execute."
        )
    else:
        next_step = (
            "loop=hitl: Present hitl_review (title, rationale, code snippets) to the user; "
            "ask proceed/skip/blocked; pass selected optional_choices into confirm_changeset. "
            "Do not decide without an explicit user reply. "
            "Or finish Approve/Skip in the queue UI, then tell you to execute the approved queue."
        )
    changeset_dump = cs.model_dump(mode="json")
    changeset_dump["targets"] = enriched_targets
    payload = {
        "done": False,
        "out_dir": str(out_dir),
        "needs_confirmation": needs_confirmation,
        "auto_proceed": auto_proceed,
        "user_decision": cs.user_decision,
        "confidence_threshold": threshold,
        "hitl_digest": digest,
        "questions": questions,
        "decisions": decisions,
        "optional_choices": optional,
        # Back-compat: decisions + optional flat list
        "hitl_choices": decisions + optional,
        "changeset": changeset_dump,
        "next_step": next_step,
    }
    if needs_confirmation and not waiting:
        payload["hitl_review"] = _hitl_review(
            cs,
            threshold=threshold,
            hitl_reason=hitl_reason,
            enriched_targets=enriched_targets,
            decisions=decisions,
            optional=optional,
        )
    fetch_ws = _verify_workspace_root(ws)
    fetch_metrics = _queue_fetch_baselines(fetch_ws, cs.targets or [])
    merged_metrics = dict(cs.metrics_delta or {})
    merged_metrics.update(fetch_metrics)
    if merged_metrics != (cs.metrics_delta or {}):
        cs = _patch_changeset(store, cs.id, metrics_delta=merged_metrics)
        changeset_dump = cs.model_dump(mode="json")
        changeset_dump["targets"] = enriched_targets
        payload["changeset"] = changeset_dump
    payload.update(
        _loop_payload(
            out_dir,
            threshold=threshold,
            current_auto_proceed=auto_proceed,
            current_waiting=waiting,
        )
    )
    return payload


def _trim_sentence(text: str, *, max_chars: int = 160) -> str:
    raw = " ".join((text or "").split())
    if len(raw) <= max_chars:
        return raw
    cut = raw[: max_chars - 1].rsplit(" ", 1)[0]
    return (cut or raw[: max_chars - 1]) + "…"


def _hitl_digest(
    cs,
    threshold: float,
    *,
    enriched_targets: list[dict[str, Any]] | None = None,
) -> str:  # noqa: ANN001
    """Short human digest for low-confidence confirm (also useful when auto_proceed)."""
    why = _trim_sentence(
        (getattr(cs, "rationale", None) or "") or (getattr(cs, "brief", None) or "")
    )
    refs: list[str] = []
    for t in cs.targets[:6]:
        loc = f"`{t.repo}/{t.path}`" if t.repo else f"`{t.path}`"
        if t.symbol:
            loc = f"{loc} · `{t.symbol}`"
        refs.append(loc)
    if len(cs.targets) > 6:
        refs.append(f"…+{len(cs.targets) - 6} more")
    ref_line = ", ".join(refs) if refs else "(no targets)"
    md = cs.metrics_delta or {}
    extras: list[str] = []
    if md.get("parent_changeset_id"):
        extras.append(f"follow-up of `{md['parent_changeset_id']}`")
    if md.get("needs_write_docs") or md.get("doc_gap_count"):
        extras.append(f"doc gaps={md.get('doc_gap_count', '?')}")
    if md.get("needs_coverage") or md.get("coverage_gap_count"):
        extras.append(f"coverage gaps={md.get('coverage_gap_count', '?')}")
    if cs.depends_on:
        extras.append(f"depends_on={','.join(cs.depends_on)}")
    extra_line = f"\nNotes: {'; '.join(extras)}" if extras else ""
    snippet_part = snippet_blocks_for_digest(enriched_targets or [])
    return (
        f"**{cs.title}** (`{cs.action}`) — confidence {cs.confidence:.2f} "
        f"(threshold {threshold:.2f})\n"
        f"{why}\n"
        f"Files: {ref_line}"
        f"{extra_line}"
        f"{snippet_part}"
    )


def _hitl_review(
    cs,  # noqa: ANN001
    *,
    threshold: float,
    hitl_reason: str | None,
    enriched_targets: list[dict[str, Any]],
    decisions: list[dict[str, str]],
    optional: list[dict[str, str]],
) -> dict[str, Any]:
    from autorefactor.report.roi import changeset_why, plain_english_upside, upside_kind

    why = changeset_why(cs, max_chars=4000)
    benefit = plain_english_upside(cs)
    kind = upside_kind(cs)
    finding_kind = str((cs.metrics_delta or {}).get("kind") or "") or None
    compare = len(enriched_targets) >= 2 and kind == "dedup"
    return {
        "title": cs.title,
        "action": cs.action,
        "rationale": why,
        "benefit": benefit if benefit and benefit != why else None,
        "confidence": cs.confidence,
        "threshold": threshold,
        "hitl_reason": hitl_reason,
        "finding_kind": finding_kind,
        "upside_kind": kind,
        "compare_targets": compare,
        "targets": enriched_targets[:8],
        "choices": decisions + optional,
        "prompt": (
            "What do you think? proceed / skip / blocked"
            + (" — compare the side-by-side snippets first" if compare else "")
        ),
    }


def _hitl_choice_groups(
    cs,
    *,
    needs_confirmation: bool,
    describe_mode: str = "raw",
    hitl_reason: str | None = None,
    threshold: float = 0.0,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    action = (cs.action or "").strip().lower()
    if action == "document":
        proceed_label = (
            "Proceed: write in-source docs for targets"
            + (
                " (applies generated descriptions)"
                if (describe_mode or "raw").strip().lower() == "describe"
                else ""
            )
        )
    elif action == "ensure_tests":
        proceed_label = "Proceed: raise characterization coverage for targets"
    else:
        proceed_label = "Proceed with the refactor (agent edits code)"
    decisions = [
        {"id": "proceed", "label": proceed_label},
        {"id": "skip", "label": "Skip this changeset"},
        {"id": "blocked", "label": "Mark blocked"},
    ]
    if needs_confirmation:
        confirm = _hitl_confirm_choice(cs, hitl_reason=hitl_reason, threshold=threshold)
        if confirm is not None:
            decisions.insert(0, confirm)
    # Prep actions ARE the work: do not bolt on redundant optionals.
    # write_docs / ensure_full_coverage are phase-level (prep CTA), not per-refactor add-ons.
    optional: list[dict[str, str]] = []
    if hitl_reason == "architectural_wiring":
        optional = [
            {
                "id": "wire_kafka_consumer",
                "label": "Wire @KafkaListener consumer for the handler",
            },
            {
                "id": "remove_dead_wiring",
                "label": "Remove dead handler and orphan PSN/Kafka config",
            },
        ]
    return decisions, optional


def _hitl_confirm_choice(
    cs,
    *,
    hitl_reason: str | None,
    threshold: float,
) -> dict[str, str] | None:
    reason = (hitl_reason or "").strip().lower()
    if reason in {"ensure_tests"}:
        return {
            "id": "confirm_ensure_tests",
            "label": "Low-confidence ensure_tests prep — confirm before scaffolding tests",
        }
    if reason == "blocked_deps":
        return {
            "id": "waiting_blocked_deps",
            "label": "Waiting on prep — drain or skip the blocking item; this is not a confirm",
        }
    if reason == "placement_risk":
        return {
            "id": "confirm_placement_risk",
            "label": (
                "Placement move injects repository/persistence deps into a data/DTO type — "
                "confirm briefly with the user"
            ),
        }
    if reason == "plan_review":
        return {
            "id": "confirm_plan_review",
            "label": (
                "Planner asked for review (uncertain keep vs trash) — "
                "confirm briefly with the user"
            ),
        }
    if reason == "framework_sensitive_split":
        return {
            "id": "confirm_framework_sensitive_split",
            "label": (
                "MapStruct @Mapper split — confirm uses/@Context wiring before applying"
            ),
        }
    if reason == "behavior_sensitive_dedup":
        return {
            "id": "confirm_behavior_sensitive",
            "label": "Behavior-sensitive merge/dedup requires confirmation",
        }
    if reason == "units_unresolved":
        return {
            "id": "confirm_units_unresolved",
            "label": "Prep targets do not resolve to index units — confirm before proceeding",
        }
    if reason == "architectural_wiring":
        return {
            "id": "confirm_architectural_wiring",
            "label": (
                "Architectural Kafka wiring — confirm wire_kafka_consumer vs remove_dead_wiring"
            ),
        }
    if reason == "security_sensitive":
        return {
            "id": "confirm_security_sensitive",
            "label": (
                "Security-sensitive item (committed secret/credential) — "
                "confirm before any document or refactor action"
            ),
        }
    if reason == "threshold_band":
        band_hi = threshold + 0.05
        return {
            "id": "confirm_threshold_band",
            "label": (
                f"Confidence {cs.confidence:.2f} is in the review band "
                f"({threshold:.2f}, {band_hi:.2f}] — confirm briefly with the user"
            ),
        }
    if reason == "low_confidence":
        return {
            "id": "confirm_low_confidence",
            "label": (
                f"Confidence {cs.confidence:.2f} is below threshold — "
                "confirm briefly with the user"
            ),
        }
    if cs.confidence < threshold:
        return {
            "id": "confirm_low_confidence",
            "label": (
                f"Confidence {cs.confidence:.2f} is below threshold — "
                "confirm briefly with the user"
            ),
        }
    return None


def tool_confirm_changeset(
    changeset_id: str,
    decision: str = "proceed",
    options: list[str] | None = None,
    out: str | None = None,
    workspace: str | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Gate: proceed/skip/blocked + optional write_docs / ensure_full_coverage."""
    proxied = _maybe_proxy(
        "confirm_changeset",
        {
            "changeset_id": changeset_id,
            "decision": decision,
            "options": options or [],
            "out": out,
            "workspace": workspace,
            "notes": notes,
        },
    )
    if proxied is not None:
        return proxied
    try:
        ws = resolve_analyze_workspace(workspace)
    except RuntimeError:
        ws = Path.cwd()
    out_dir = resolve_out_dir(ws, Path(out) if out else None)
    store = _queue_store(out_dir)
    item = store.get(changeset_id)
    if item is None:
        return {"ok": False, "error": "not_found", "out_dir": str(out_dir)}

    config = load_merged_config(workspace=ws, out_dir=out_dir)
    threshold = float(config.confidence_threshold)

    decision_l = (decision or "proceed").strip().lower()
    opts = {o.strip().lower().replace("-", "_") for o in (options or []) if o and str(o).strip()}
    # aliases
    if "full_coverage" in opts or "ensure_tests" in opts:
        opts.add("ensure_full_coverage")
    # Prep items: proceed implies the named work (no confusing bolt-on optional).
    action_l = (item.action or "").strip().lower()
    describe_mode = config.resolved_describe_mode()
    if decision_l in {"proceed", "yes", "y", "auto", "auto_proceed"}:
        if action_l == "document" and describe_mode == "describe":
            opts.add("write_docs")
        if action_l == "ensure_tests":
            opts.add("ensure_full_coverage")
    touched: list[str] = []
    docs_written = 0
    agent_test_brief: str | None = None

    def _with_loop(payload: dict[str, Any]) -> dict[str, Any]:
        payload.update(_loop_payload(out_dir, threshold=threshold))
        loop = payload["loop"]
        if loop == "autorun":
            payload["next_step"] = (
                "Do not ask the user. Immediately call get_next_changeset and continue "
                "the autorun streak."
            )
        elif loop == "hitl":
            payload.setdefault(
                "next_step",
                "Call get_next_changeset; show hitl_digest and ask the user before confirming.",
            )
        else:
            payload.setdefault("next_step", "Changeset queue empty.")
        return payload

    if decision_l in {"skip", "skipped"}:
        updated = store.mark(changeset_id, ChangesetStatus.SKIPPED, notes=notes)
        from autorefactor.freshness import advance_run_fingerprint
        from autorefactor.queue import load_queue, save_queue

        advance_run_fingerprint(out_dir, ws)
        save_queue(out_dir, load_queue(out_dir))
        return _with_loop(
            {
                "ok": True,
                "out_dir": str(out_dir),
                "decision": "skip",
                "changeset": updated.model_dump(mode="json"),
                "touched_paths": [],
                "docs_written": 0,
            }
        )
    if decision_l in {"blocked", "block"}:
        updated = store.mark(changeset_id, ChangesetStatus.BLOCKED, notes=notes)
        from autorefactor.queue import load_queue, save_queue

        save_queue(out_dir, load_queue(out_dir))
        return _with_loop(
            {
                "ok": True,
                "out_dir": str(out_dir),
                "decision": "blocked",
                "changeset": updated.model_dump(mode="json"),
                "touched_paths": [],
                "docs_written": 0,
            }
        )

    if "write_docs" in opts:
        doc_paths = _hitl_write_docs(ws, out_dir, item)
        touched.extend(doc_paths)
        docs_written = len(doc_paths)

    if "ensure_full_coverage" in opts:
        test_paths, agent_test_brief = _hitl_ensure_full_coverage(ws, out_dir, item)
        for p in test_paths:
            if p not in touched:
                touched.append(p)

    _sync_local_before_mark_verify()
    verify_ws = _verify_workspace_root(ws)

    from autorefactor.freshness import confirm_apply_baselines, target_paths_fingerprint

    confirm_metrics = dict(item.metrics_delta or {})
    pre_confirm_warnings: list[str] = []
    fetch_target_fp = confirm_metrics.get("queue_fetch_target_fingerprint")
    if fetch_target_fp and item.targets:
        current_target_fp = target_paths_fingerprint(verify_ws, item.targets)
        if current_target_fp != fetch_target_fp:
            confirm_metrics["pre_confirm_apply"] = True
            pre_confirm_warnings.append(
                "Target paths already differ from queue-fetch baseline — "
                "local edits were applied before confirm."
            )
    confirm_metrics.update(confirm_apply_baselines(verify_ws, item.targets or []))

    note = notes or (
        "Confirmed — agent should apply refactor"
        if decision_l in {"proceed", "yes", "y", "auto", "auto_proceed"}
        else notes
    )
    updated = _patch_changeset(
        store,
        changeset_id,
        status=ChangesetStatus.IN_PROGRESS,
        notes=note,
        metrics_delta=confirm_metrics,
    )
    from autorefactor.queue import load_queue, save_queue

    save_queue(out_dir, load_queue(out_dir))
    next_bits = [
        "Apply the refactor in the IDE, then mark_changeset status=done (or skip/blocked)."
    ]
    if agent_test_brief:
        next_bits.append(agent_test_brief)
    payload = {
        "ok": True,
        "out_dir": str(out_dir),
        "decision": "proceed",
        "options_applied": sorted(opts),
        "changeset": updated.model_dump(mode="json"),
        "touched_paths": touched,
        "docs_written": docs_written,
        "agent_test_brief": agent_test_brief,
        "next_step": " ".join(next_bits),
        "loop": "autorun" if item.confidence >= threshold else "hitl",
        "queue_counts": _queue_counts(out_dir, threshold=threshold),
    }
    if pre_confirm_warnings:
        payload["warnings"] = pre_confirm_warnings
    return payload


def _resolve_changeset_units(out_dir: Path, item) -> list:  # noqa: ANN001
    from autorefactor.artifacts import load_units

    units = load_units(layout_for(out_dir))
    by_id = {u.id: u for u in units}
    resolved = []
    for target in item.targets:
        unit = by_id.get(target.unit_id) if target.unit_id else None
        if unit is None:
            unit = next(
                (
                    u
                    for u in units
                    if u.repo == target.repo
                    and u.path == target.path
                    and (not target.symbol or target.symbol in (u.qualified_name or ""))
                ),
                None,
            )
        if unit is not None:
            resolved.append(unit)
    return resolved


def _hitl_write_docs(workspace: Path, out_dir: Path, item) -> list[str]:  # noqa: ANN001
    from autorefactor.domain import DocumentGap
    from autorefactor.stages.document import write_unit_doc

    touched: list[str] = []
    for unit in _resolve_changeset_units(out_dir, item):
        gap = DocumentGap(
            unit_id=unit.id,
            repo=unit.repo,
            path=unit.path,
            qualified_name=unit.qualified_name,
            language=unit.language,
            kind=unit.kind.value if hasattr(unit.kind, "value") else str(unit.kind),
            reason="HITL write_docs",
        )
        path = write_unit_doc(workspace.resolve(), gap, unit)
        if path:
            touched.append(path)
    return touched


def _hitl_ensure_full_coverage(
    workspace: Path, out_dir: Path, item
) -> tuple[list[str], str]:  # noqa: ANN001
    """Scaffold characterization stubs where possible; agent must maximize path coverage."""
    from autorefactor.artifacts import load_units
    from autorefactor.domain import TestGap, TestPlan, UnitKind
    from autorefactor.testing.gaps import (
        is_test_target_unit,
        resolve_production_units_for_ensure_tests,
        suggested_test_path,
    )
    from autorefactor.testing.inventory import inventory_test_files, looks_like_test_path
    from autorefactor.testing.stubs import write_skip_llm_test_stubs

    units = _resolve_changeset_units(out_dir, item)
    all_units = load_units(layout_for(out_dir))
    # Remap padding / test-path targets to production units (Janus BonusModelCoverageTest).
    if any(is_test_target_unit(u) or looks_like_test_path(u.path) for u in units):
        remapped = resolve_production_units_for_ensure_tests(
            units,
            all_units or units,
            coverage=None,
            evidence_text=getattr(item, "rationale", None) or getattr(item, "brief", None) or "",
        )
        if remapped:
            units = remapped
        else:
            units = [u for u in units if not is_test_target_unit(u) and not looks_like_test_path(u.path)]
    repos = sorted({workspace / u.repo for u in units if u.repo})
    if not repos:
        repos = [workspace]
    existing = inventory_test_files(repos)
    existing_lower = {p.lower() for p in existing}
    gaps: list[TestGap] = []
    target_refs: list[str] = []
    for unit in units:
        if unit.kind not in {
            UnitKind.FUNCTION,
            UnitKind.TYPE,
            UnitKind.PROTO_MESSAGE,
            UnitKind.PROTO_RPC,
            UnitKind.PROTO_SERVICE,
        }:
            continue
        if looks_like_test_path(unit.path) or is_test_target_unit(unit):
            continue
        suggested = suggested_test_path(unit)
        target_refs.append(f"`{unit.repo}/{unit.path}` · `{unit.qualified_name}`")
        if suggested.lower() in existing_lower:
            continue
        gaps.append(
            TestGap(
                unit_id=unit.id,
                repo=unit.repo,
                path=unit.path,
                qualified_name=unit.qualified_name,
                language=unit.language,
                reason="HITL ensure_full_coverage",
                suggested_test_path=suggested,
            )
        )
    plan = TestPlan(
        summary=f"HITL ensure_full_coverage: {len(gaps)} gap(s) for changeset targets",
        gaps=gaps,
        existing_test_files=existing,
    )
    created = write_skip_llm_test_stubs(workspace.resolve(), plan, units)
    refs = ", ".join(target_refs[:8]) or "(no production units resolved)"
    brief = (
        "ensure_full_coverage: expand any scaffolds and write tests aiming at the "
        "highest practical path coverage for: "
        f"{refs}. Cover branches/error paths; run the repo test command; "
        "Do not expand Jacoco padding / symbol-exists-only tests. "
        "do not claim coverage the runner did not measure."
    )
    if created:
        brief += f" Scaffolds written: {', '.join(f'`{p}`' for p in created)}."
    return created, brief


def tool_get_changeset(
    changeset_id: str,
    out: str | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    proxied = _maybe_proxy(
        "get_changeset",
        {"changeset_id": changeset_id, "out": out, "workspace": workspace},
    )
    if proxied is not None:
        return proxied
    try:
        ws = resolve_analyze_workspace(workspace)
    except RuntimeError:
        ws = Path.cwd()
    out_dir = resolve_out_dir(ws, Path(out) if out else None)
    item = _queue_store(out_dir).get(changeset_id)
    if item is None:
        return {"ok": False, "error": "not_found", "out_dir": str(out_dir)}
    enriched = enrich_targets(out_dir, item.targets or [])
    changeset_dump = item.model_dump(mode="json")
    changeset_dump["targets"] = enriched
    return {"ok": True, "out_dir": str(out_dir), "changeset": changeset_dump}


_STRUCTURAL_VERIFY_ACTIONS = frozenset({"move", "extract_shared", "delete_duplicate"})
_PARTIAL_APPLY_MARKERS = (
    "partial apply",
    "partially applied",
    "partial completion",
    "not fully applied",
    "acknowledge partial",
)
_OUTCOME_SYMBOL_PATTERNS = (
    re.compile(
        r"(?:moved|relocated|extracted|consolidated)\s+(?:[\w/`'.\s]+?\s+)?(?:to|into)\s+[`']?([\w][\w.]*)",
        re.IGNORECASE,
    ),
    re.compile(r"→\s*[`']?([\w][\w.]*)"),
    re.compile(r"->\s*[`']?([\w][\w.]*)"),
    re.compile(
        r"\b(?:new|added)\s+(?:method|symbol|function)\s+[`']?([\w][\w.]*)",
        re.IGNORECASE,
    ),
)


def _notes_acknowledge_partial_apply(notes: str) -> bool:
    lower = (notes or "").lower()
    return any(marker in lower for marker in _PARTIAL_APPLY_MARKERS)


def _extract_claimed_outcome_symbols(text: str) -> set[str]:
    symbols: set[str] = set()
    for pattern in _OUTCOME_SYMBOL_PATTERNS:
        for match in pattern.finditer(text or ""):
            sym = (match.group(1) or "").strip("`'\"").strip()
            if not sym:
                continue
            if len(sym.rsplit(".", 1)[-1]) < 3:
                continue
            symbols.add(sym)
    return symbols


def _symbol_present_in_units(symbol: str, units: list) -> bool:  # noqa: ANN001
    sym = symbol.strip()
    if not sym:
        return True
    simple = sym.rsplit(".", 1)[-1]
    for unit in units:
        qn = unit.qualified_name or ""
        if sym in qn or qn.endswith("." + simple) or qn == simple:
            return True
        snippet = unit.source_snippet or ""
        if re.search(rf"\b{re.escape(simple)}\s*\(", snippet):
            return True
        if simple[0].islower() and re.search(rf"\b{re.escape(simple)}\b", snippet):
            return True
    return False


def _resolve_changeset_target_units(changeset, units: list) -> list:  # noqa: ANN001
    by_id = {u.id: u for u in units}
    resolved = []
    seen: set[str] = set()
    for target in changeset.targets or []:
        unit = by_id.get(target.unit_id) if target.unit_id else None
        if unit is None:
            unit = next(
                (
                    u
                    for u in units
                    if u.repo == target.repo
                    and u.path == target.path
                    and (not target.symbol or target.symbol in (u.qualified_name or ""))
                ),
                None,
            )
        if unit is not None and unit.id not in seen:
            seen.add(unit.id)
            resolved.append(unit)
    return resolved


def _extract_shared_handlers_still_cloned(changeset, units: list) -> bool:  # noqa: ANN001
    """True when extract_shared targets remain structurally duplicated after apply."""
    action = (changeset.action or "").strip().lower()
    if action != "extract_shared":
        return False
    from autorefactor.domain import UnitKind
    from autorefactor.sensors.similarity import (
        _distinct_exception_handler_pair,
        shape_similarity,
    )

    target_units = [
        u
        for u in _resolve_changeset_target_units(changeset, units)
        if u.kind == UnitKind.FUNCTION
    ]
    if len(target_units) < 2:
        return False
    blob = " ".join(
        filter(None, [changeset.title or "", changeset.rationale or "", changeset.brief or ""])
    ).lower()
    handler_context = "@exceptionhandler" in blob or "exceptionhandler" in blob
    if not handler_context and not any(
        _distinct_exception_handler_pair(target_units[i], target_units[j])
        for i in range(len(target_units))
        for j in range(i + 1, len(target_units))
    ):
        return False
    for i in range(len(target_units)):
        for j in range(i + 1, len(target_units)):
            a, b = target_units[i], target_units[j]
            if _distinct_exception_handler_pair(a, b):
                continue
            sim = shape_similarity(a.source_snippet or "", b.source_snippet or "")
            if sim >= 0.65:
                return True
    return False


def _verify_structural_apply(
    out_dir: Path,
    changeset,
    *,
    notes: str,
) -> dict[str, Any] | None:
    """Return error payload when promised symbols from notes/brief are missing."""
    action = (changeset.action or "").strip().lower()
    if action not in _STRUCTURAL_VERIFY_ACTIONS:
        return None
    if _notes_acknowledge_partial_apply(notes):
        return None

    blob = " ".join(
        filter(None, [notes, changeset.brief, changeset.rationale, changeset.title])
    )
    claimed = _extract_claimed_outcome_symbols(blob)
    if not claimed:
        return None

    from autorefactor.artifacts import load_units

    units = load_units(layout_for(out_dir))
    missing = sorted(s for s in claimed if not _symbol_present_in_units(s, units))
    if missing:
        return {
            "ok": False,
            "error": "verification_failed",
            "detail": (
                "Promised symbol(s) not found in indexed workspace: "
                + ", ".join(missing)
            ),
            "missing_symbols": missing,
            "out_dir": str(out_dir),
            "changeset_id": changeset.id,
        }

    if _extract_shared_handlers_still_cloned(changeset, units):
        return {
            "ok": False,
            "error": "verification_failed",
            "detail": (
                "extract_shared incomplete: target handler bodies remain structurally "
                "duplicated — refactor handlers to call the shared helper before marking done."
            ),
            "out_dir": str(out_dir),
            "changeset_id": changeset.id,
        }

    return None


def _queue_fetch_baselines(workspace: Path, targets: list) -> dict[str, str]:
    from autorefactor.freshness import target_paths_fingerprint, workspace_content_fingerprint

    return {
        "queue_fetch_workspace_fingerprint": workspace_content_fingerprint(workspace),
        "queue_fetch_target_fingerprint": target_paths_fingerprint(workspace, targets),
    }


def _patch_changeset(store: ChangesetStore, changeset_id: str, **updates: Any) -> Changeset:
    items = store.load()
    for idx, item in enumerate(items):
        if item.id == changeset_id:
            updated = item.model_copy(update=updates)
            items[idx] = updated
            store.save(items)
            return updated
    raise KeyError(f"changeset not found: {changeset_id}")


def _sync_local_before_mark_verify() -> None:
    """Upload local IDE edits before done-mark fingerprint checks (remote MCP queue walk)."""
    from autorefactor.mcp.remote import ensure_remote_workspace, remote_enabled

    if not remote_enabled():
        return
    try:
        ensure_remote_workspace(force=True)
    except Exception:  # noqa: BLE001
        pass


def _verify_workspace_root(workspace: Path) -> Path:
    """Prefer the local project root for apply verification when proxying to remote."""
    from autorefactor.mcp.remote import _local_workspace_root, remote_enabled

    if remote_enabled():
        return _local_workspace_root()
    return workspace


def _verify_workspace_apply(
    workspace: Path,
    out_dir: Path,
    changeset,
    *,
    notes: str,
    marking_done: bool,
) -> dict[str, Any] | None:
    """Reject done marks when workspace/target paths match the post-confirm baseline."""
    if not marking_done:
        return None
    if _notes_acknowledge_partial_apply(notes):
        return None

    metrics = changeset.metrics_delta or {}
    if metrics.get("pre_confirm_apply"):
        return None
    ws_fp = metrics.get("confirm_workspace_fingerprint")
    target_fp = metrics.get("confirm_target_fingerprint")
    if not ws_fp and not target_fp:
        identity = load_run_identity(out_dir)
        ws_fp = identity.get("content_fingerprint")

    from autorefactor.freshness import workspace_apply_changed

    changed = workspace_apply_changed(
        workspace,
        baseline_workspace_fp=str(ws_fp) if ws_fp else None,
        baseline_target_fp=str(target_fp) if target_fp else None,
        targets=changeset.targets or [],
    )
    if changed:
        if target_fp and changeset.targets:
            from autorefactor.freshness import target_paths_fingerprint

            current_target_fp = target_paths_fingerprint(workspace, changeset.targets)
            if current_target_fp == target_fp:
                return {
                    "ok": False,
                    "error": "no_workspace_change",
                    "detail": (
                        "Target paths match the post-confirm baseline — apply edits to "
                        "changeset targets before marking done."
                    ),
                    "out_dir": str(out_dir),
                    "changeset_id": changeset.id,
                }
        return None

    if changeset.status == ChangesetStatus.DONE:
        detail = "Changeset is already done and the workspace matches the last baseline."
    else:
        detail = (
            "Workspace fingerprint matches the post-confirm baseline and target paths "
            "are unchanged — apply the refactor before marking done."
        )
    return {
        "ok": False,
        "error": "no_workspace_change",
        "detail": detail,
        "out_dir": str(out_dir),
        "changeset_id": changeset.id,
    }


def tool_mark_changeset(
    changeset_id: str,
    status: str,
    out: str | None = None,
    workspace: str | None = None,
    notes: str = "",
) -> dict[str, Any]:
    proxied = _maybe_proxy(
        "mark_changeset",
        {
            "changeset_id": changeset_id,
            "status": status,
            "out": out,
            "workspace": workspace,
            "notes": notes,
        },
    )
    if proxied is not None:
        return proxied
    try:
        ws = resolve_analyze_workspace(workspace)
    except RuntimeError:
        ws = Path.cwd()
    out_dir = resolve_out_dir(ws, Path(out) if out else None)
    store = _queue_store(out_dir)
    item = store.get(changeset_id)
    if item is None:
        return {"ok": False, "error": "not_found", "out_dir": str(out_dir)}
    status_enum = ChangesetStatus(status)
    if status_enum == ChangesetStatus.DONE:
        _sync_local_before_mark_verify()
        try:
            ws = resolve_analyze_workspace(workspace)
        except RuntimeError:
            ws = Path.cwd()
        verify_ws = _verify_workspace_root(ws)
        verify_error = _verify_structural_apply(out_dir, item, notes=notes)
        if verify_error is not None:
            return verify_error
        workspace_error = _verify_workspace_apply(
            verify_ws,
            out_dir,
            item,
            notes=notes,
            marking_done=True,
        )
        if workspace_error is not None:
            return workspace_error
    updated = store.mark(changeset_id, status_enum, notes=notes)
    # Keep v2 + compat queues aligned after status transitions
    from autorefactor.freshness import advance_run_fingerprint
    from autorefactor.queue import load_queue, save_queue

    if status_enum in {ChangesetStatus.DONE, ChangesetStatus.SKIPPED}:
        advance_run_fingerprint(out_dir, ws)
    save_queue(out_dir, load_queue(out_dir))
    config = load_merged_config(workspace=ws, out_dir=out_dir)
    threshold = float(config.confidence_threshold)
    payload: dict[str, Any] = {
        "ok": True,
        "out_dir": str(out_dir),
        "changeset": updated.model_dump(mode="json"),
    }
    payload.update(_loop_payload(out_dir, threshold=threshold))
    if payload["loop"] == "autorun":
        payload["next_step"] = (
            "Do not ask the user. Immediately call get_next_changeset and continue "
            "the autorun streak."
        )
    elif payload["loop"] == "hitl":
        payload["next_step"] = (
            "Call get_next_changeset; show hitl_digest and ask the user before confirming. "
            "Or finish Approve/Skip in the queue UI, then tell you to execute."
        )
    else:
        suite_hint = (
            "Before claiming done: run the project test suite once "
            "(mvn test / pytest / npm test — whatever the repo uses) and report results."
        )
        if payload["loop"] == "done" or status_enum == ChangesetStatus.DONE:
            payload["next_step"] = f"Changeset queue empty or autorun streak ended. {suite_hint}"
        else:
            payload["next_step"] = f"Changeset queue empty. {suite_hint}"
    return payload


def tool_describe_unit(
    workspace: str | None = None,
    unit_id: str = "",
    out: str | None = None,
    skip_llm: bool = False,
) -> dict[str, Any]:
    from autorefactor.artifacts import load_units

    proxied = _maybe_proxy(
        "describe_unit",
        {"workspace": workspace, "unit_id": unit_id, "out": out, "skip_llm": skip_llm},
    )
    if proxied is not None:
        return proxied
    try:
        ws = resolve_analyze_workspace(workspace)
    except RuntimeError as exc:
        return {"ok": False, "error": "no_workspace", "detail": str(exc)}
    out_dir = resolve_out_dir(ws, Path(out) if out else None)
    config = load_merged_config(workspace=ws, out_dir=out_dir)
    config.skip_llm = skip_llm or config.skip_llm
    units = load_units(layout_for(out_dir))
    unit = next((u for u in units if u.id == unit_id), None)
    if unit is None:
        return {"ok": False, "error": "unit_not_found", "out_dir": str(out_dir)}
    provider = build_provider(config)
    store = IndexStore(out_dir / "cache" / "index.db")
    described = describe_units([unit], provider, store, concurrency=1)
    store.close()
    u = described[0]
    return {
        "ok": True,
        "out_dir": str(out_dir),
        "unit_id": u.id,
        "description": u.description,
        "tags": u.tags,
        "note": (
            "Analysis-index metadata only; does not write source Javadocs or add tests. "
            "Edit code with the host agent if needed."
        ),
    }


def _enrich_store(out_dir: Path):
    from autorefactor.enrich_queue.store import EnrichTaskStore, enrich_queue_path

    return EnrichTaskStore(enrich_queue_path(out_dir))


def tool_get_agent_playbook(workspace: str | None = None, out: str | None = None) -> dict[str, Any]:
    """Preset-aware workflow details (MCP initialize already ships core instructions)."""
    proxied = _maybe_proxy("get_agent_playbook", {"workspace": workspace, "out": out})
    if proxied is not None:
        return proxied
    try:
        ws = resolve_analyze_workspace(workspace)
    except RuntimeError:
        ws = Path.cwd()
    out_dir = resolve_out_dir(ws, Path(out) if out else None)
    config = load_merged_config(workspace=ws, out_dir=out_dir)
    from autorefactor.mcp.guidance import build_agent_guidance

    guidance = build_agent_guidance(config)
    return {
        "ok": True,
        "always_call_first": False,
        "instructions_note": (
            "Core workflow is in MCP InitializeResult.instructions; "
            "this tool returns live chat/embed axes and setup detail."
        ),
        "chat_backend": guidance["chat_backend"],
        "embed_backend": guidance["embed_backend"],
        "parallel_llm": guidance["parallel_llm"],
        "describe_mode": guidance["describe_mode"],
        "embed_batch_size": guidance.get("embed_batch_size"),
        "embed_concurrency": guidance.get("embed_concurrency"),
        "effective_embed_parallel": guidance.get("effective_embed_parallel"),
        "profile": guidance["profile"],
        "workflow": guidance["workflow"],
        "forbidden": guidance["forbidden"],
        "freshness": guidance["freshness"],
        "tools": guidance["tools"],
        "submit_enrich_examples": {
            "describe": {"doc": "40-80 word behavioral summary…", "tags": ["domain"]},
            "plan": {
                "decisions": [
                    {
                        "title": "Extract shared helper",
                        "action": "extract_shared",
                        "rationale": "…",
                        "confidence": 0.7,
                        "finding_ids": ["…"],
                        "unit_ids": ["…"],
                        "valid": True,
                    }
                ]
            },
        },
        "next_step": "Call get_status; analyze if stale or refresh requested; else follow queue workflow.",
        "needs_setup": guidance["needs_setup"],
        "setup_hint": guidance.get("setup_hint"),
        "setup": guidance["setup"],
    }


def tool_list_enrich_tasks(out: str | None = None, workspace: str | None = None) -> dict[str, Any]:
    proxied = _maybe_proxy("list_enrich_tasks", {"out": out, "workspace": workspace})
    if proxied is not None:
        return proxied
    try:
        ws = resolve_analyze_workspace(workspace)
    except RuntimeError:
        ws = Path.cwd()
    out_dir = resolve_out_dir(ws, Path(out) if out else None)
    items = _enrich_store(out_dir).list()
    return {
        "ok": True,
        "out_dir": str(out_dir),
        "enrich_tasks": [
            {
                "id": t.id,
                "kind": t.kind.value,
                "status": t.status.value,
                "priority": t.priority,
                "notes": t.notes,
            }
            for t in items
        ],
    }


def tool_get_next_enrich_task(
    out: str | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    proxied = _maybe_proxy("get_next_enrich_task", {"out": out, "workspace": workspace})
    if proxied is not None:
        return proxied
    try:
        ws = resolve_analyze_workspace(workspace)
    except RuntimeError:
        ws = Path.cwd()
    out_dir = resolve_out_dir(ws, Path(out) if out else None)
    store = _enrich_store(out_dir)
    result = store.next_pending()
    if result.done or result.task is None:
        return {
            "done": True,
            "out_dir": str(out_dir),
            "next_step": "Call get_report, then get_next_changeset.",
        }
    task = result.task
    if task.status.value == "pending":
        from autorefactor.enrich_queue.models import EnrichTaskStatus

        store.mark(task.id, EnrichTaskStatus.IN_PROGRESS)
        task = store.get(task.id) or task
    return {
        "done": False,
        "out_dir": str(out_dir),
        "task": task.model_dump(mode="json"),
        "next_step": "Complete the task, then submit_enrich_result with the JSON result.",
    }


def tool_submit_enrich_result(
    task_id: str,
    result: dict[str, Any],
    out: str | None = None,
    workspace: str | None = None,
    notes: str = "",
) -> dict[str, Any]:
    proxied = _maybe_proxy(
        "submit_enrich_result",
        {
            "task_id": task_id,
            "result": result,
            "out": out,
            "workspace": workspace,
            "notes": notes,
        },
    )
    if proxied is not None:
        return proxied
    from autorefactor.enrich_queue.models import EnrichTaskStatus
    from autorefactor.enrich_queue.store import validate_enrich_result

    try:
        ws = resolve_analyze_workspace(workspace)
    except RuntimeError:
        ws = Path.cwd()
    out_dir = resolve_out_dir(ws, Path(out) if out else None)
    store = _enrich_store(out_dir)
    task = store.get(task_id)
    if task is None:
        return {"ok": False, "error": "not_found", "out_dir": str(out_dir)}
    try:
        normalized = validate_enrich_result(task.kind, result or {})
    except (ValueError, Exception) as exc:  # noqa: BLE001
        store.mark(
            task_id,
            EnrichTaskStatus.PENDING,
            notes=f"validation_failed: {exc}"[:500],
        )
        return {
            "ok": False,
            "error": "validation_failed",
            "detail": str(exc)[:500],
            "out_dir": str(out_dir),
            "task_id": task_id,
        }
    updated = store.mark(
        task_id,
        EnrichTaskStatus.DONE,
        notes=notes,
        result=normalized,
    )
    queue_applied: dict[str, Any] | None = None
    # Apply describe results into the index when possible
    if task.kind.value == "describe":
        unit_id = (task.payload or {}).get("unit_id")
        doc = normalized.get("doc") or normalized.get("description")
        if unit_id and doc:
            try:
                from autorefactor.artifacts import load_units, save_index_units

                layout = layout_for(out_dir)
                units = load_units(layout)
                changed = False
                new_units = []
                for u in units:
                    if u.id == unit_id:
                        new_units.append(
                            u.model_copy(
                                update={
                                    "description": doc,
                                    "tags": normalized.get("tags") or u.tags,
                                }
                            )
                        )
                        changed = True
                    else:
                        new_units.append(u)
                if changed:
                    save_index_units(layout, new_units)
            except Exception:  # noqa: BLE001
                pass
    elif task.kind.value == "plan":
        from autorefactor.enrich_queue.apply_plan import apply_plan_enrich_to_queue

        queue_applied = apply_plan_enrich_to_queue(
            out_dir, list(normalized.get("decisions") or [])
        )
    from autorefactor.enrich_queue import pending_enrich_count

    pending = pending_enrich_count(out_dir)
    payload: dict[str, Any] = {
        "ok": True,
        "out_dir": str(out_dir),
        "task": updated.model_dump(mode="json"),
        "enrich_pending": pending,
        "next_step": (
            "Call get_next_enrich_task."
            if pending
            else "Enrich queue empty — call get_report, then get_next_changeset."
        ),
    }
    if queue_applied is not None:
        payload["queue_applied"] = queue_applied
    return payload
