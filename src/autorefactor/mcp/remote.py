from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from autorefactor.sync_client import pull_workspace_http, sync_workspace_http

_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

_session_id: str | None = None
_req_id = 0
_last_sync_fingerprint: str | None = None
_last_sync_at: float = 0.0
_last_sync_root: Path | None = None
_pulled_job_ids: set[str] = set()


class SyncRootError(RuntimeError):
    """Local project path for upload could not be resolved."""


_PROJECT_MARKERS = (
    "pyproject.toml",
    "pom.xml",
    "package.json",
    "go.mod",
    "build.gradle",
    "build.gradle.kts",
    "Cargo.toml",
    "Gemfile",
    "composer.json",
)


def remote_base_url() -> str | None:
    raw = (os.environ.get("AUTOREFACTOR_REMOTE_URL") or "").strip().rstrip("/")
    if not raw:
        return None
    return raw


def remote_enabled() -> bool:
    return remote_base_url() is not None


def _mcp_endpoint(base: str) -> str:
    if base.endswith("/mcp"):
        return base
    return f"{base}/mcp"


def _api_base(base: str) -> str:
    if base.endswith("/mcp"):
        return base[: -len("/mcp")] or base
    return base


_EVAL_SNAPSHOT = ".ar_eval_snapshot.json"


def _is_eval_subject_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    if (path / _EVAL_SNAPSHOT).is_file():
        return True
    parts = path.resolve().parts
    if "eval" in parts and "subjects" in parts:
        idx = parts.index("subjects")
        if idx > 0 and parts[idx - 1] == "eval" and idx + 1 < len(parts):
            return True
    return False


def _discover_eval_subject(start: Path) -> Path | None:
    cur = start.resolve()
    for _ in range(32):
        if _is_eval_subject_dir(cur):
            return cur
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return None


def _is_product_checkout(path: Path) -> bool:
    """True when ``path`` is the autorefactor product repo, not an eval subject copy."""
    root = path.resolve()
    if _is_eval_subject_dir(root):
        return False
    parts = root.parts
    if "eval" in parts and "subjects" in parts:
        return False
    return (root / "src" / "autorefactor").is_dir() and (root / "pyproject.toml").is_file()


def _remote_workspace_for_local(local_root: Path, workspace: str | None = None) -> str:
    """Map host workspace intent to a path the remote engine can resolve.

    The autorefactor product repo is bind-mounted at ``/workspace`` in Docker.
    Do not analyze stale ``active`` sync copies (demo eval subjects) for it.
    Other projects are tar-synced into ``active`` on the remote.
    Host-only absolute paths are never forwarded — remote cannot see them.
    """
    raw = (workspace or "").strip()
    if _is_product_checkout(local_root):
        return "mounted"
    if raw in {"active", "synced", "remote"}:
        return raw
    if raw in {"", "default", ".", "mounted"}:
        return "active"
    if raw and Path(raw).expanduser().is_absolute():
        return "active"
    return "active"


def _sync_skipped_meta(local_root: Path, *, reason: str) -> dict[str, Any]:
    return {
        "ok": True,
        "skipped": True,
        "reason": reason,
        "root": str(local_root),
        "label": local_root.name,
        "source": str(local_root),
        "pack": {"file_count": 0},
    }


def _has_project_markers(path: Path) -> bool:
    root = path.resolve()
    if _is_product_checkout(root) or _is_eval_subject_dir(root):
        return True
    if any((root / name).exists() for name in _PROJECT_MARKERS):
        return True
    if (root / ".git").exists():
        return True
    try:
        return any(root.iterdir())
    except OSError:
        return False


def _resolve_workspace_candidate(path: Path) -> Path:
    """Normalize a directory to the tree that should be packed."""
    root = path.resolve()
    if not root.is_dir():
        raise SyncRootError(f"path is not a directory: {root}")
    if _is_product_checkout(root):
        return root
    discovered = _discover_eval_subject(root)
    if discovered is not None:
        return discovered
    return root


def resolve_sync_root(path: str | None = None) -> Path:
    """Resolve the local project directory to upload to the remote engine.

    Priority: explicit ``path`` argument > ``AUTOREFACTOR_MCP_WORKSPACE`` > cwd.
    Does not depend on git or any IDE-specific environment.
    """
    raw = (path or "").strip()
    if raw:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = (Path.cwd() / candidate).resolve()
        else:
            candidate = candidate.resolve()
        return _resolve_workspace_candidate(candidate)

    env = (os.environ.get("AUTOREFACTOR_MCP_WORKSPACE") or "").strip()
    if env:
        candidate = Path(env).expanduser().resolve()
        if not candidate.is_dir():
            raise SyncRootError(f"AUTOREFACTOR_MCP_WORKSPACE is not a directory: {candidate}")
        return _resolve_workspace_candidate(candidate)

    cwd = Path.cwd().resolve()
    if not cwd.is_dir():
        raise SyncRootError(
            "No sync path: pass path='/absolute/path/to/project' or set AUTOREFACTOR_MCP_WORKSPACE."
        )
    if cwd == Path.home().resolve() or not _has_project_markers(cwd):
        raise SyncRootError(
            "No sync path: pass path='/absolute/path/to/project' "
            f"(cwd {cwd} is not a recognizable project root)."
        )
    return _resolve_workspace_candidate(cwd)


def _local_workspace_root(path: str | None = None) -> Path:
    """Backward-compatible alias for :func:`resolve_sync_root`."""
    return resolve_sync_root(path)


def _extract_sync_path(args: dict[str, Any]) -> str | None:
    """Read explicit local path from tool args (``path`` or absolute ``workspace``)."""
    explicit = str(args.get("path") or "").strip()
    if explicit:
        return explicit
    ws = str(args.get("workspace") or "").strip()
    if ws:
        candidate = Path(ws).expanduser()
        if candidate.is_absolute() and candidate.is_dir():
            return ws
    return None


def _resolve_proxy_local_root(args: dict[str, Any]) -> Path:
    """Resolve local upload root for a proxied tool call."""
    global _last_sync_root
    explicit = _extract_sync_path(args)
    if explicit:
        root = resolve_sync_root(explicit)
        _last_sync_root = root
        return root
    if _last_sync_root is not None:
        return _last_sync_root
    root = resolve_sync_root(None)
    _last_sync_root = root
    return root


def _local_pack_identity(root: Path, sync_meta: dict[str, Any]) -> dict[str, Any]:
    pack = sync_meta.get("pack") if isinstance(sync_meta.get("pack"), dict) else {}
    file_count = int(sync_meta.get("file_count") or pack.get("file_count") or 0)
    if file_count <= 0:
        try:
            from autorefactor.workspace_sync import list_workspace_files

            file_count = len(list_workspace_files(root))
        except Exception:  # noqa: BLE001
            file_count = 0
    return {
        "label": str(sync_meta.get("label") or root.name),
        "source": str(sync_meta.get("source") or root),
        "file_count": file_count,
    }


def _remote_workspace_identity() -> dict[str, Any] | None:
    try:
        # Fail-fast: workspace_status is used for proxy routing decisions.
        # Avoid blocking the whole MCP call if the remote is slow/unreachable.
        status = call_remote_tool("workspace_status", {}, timeout=10.0)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(status, dict):
        return None
    return {
        "label": str(status.get("label") or status.get("sync_label") or ""),
        "source": str(status.get("source") or status.get("sync_source") or ""),
        "file_count": int(status.get("file_count") or 0),
    }


def _workspace_mismatch(local: dict[str, Any], remote: dict[str, Any]) -> bool:
    """True only when label or source disagree — file_count drift alone is tolerated."""
    for key in ("label", "source"):
        lv = str(local.get(key) or "")
        rv = str(remote.get(key) or "")
        if rv and lv and rv != lv:
            return True
    return False


def _file_count_drift(
    local: dict[str, Any],
    remote: dict[str, Any],
    *,
    tolerance: float = 0.05,
) -> bool:
    lv_fc = int(local.get("file_count") or 0)
    rv_fc = int(remote.get("file_count") or 0)
    if lv_fc <= 0 or rv_fc <= 0 or lv_fc == rv_fc:
        return False
    base = max(lv_fc, rv_fc)
    return abs(lv_fc - rv_fc) / base > tolerance


def _fingerprint(root: Path) -> str:
    """Fast path+size+mtime fingerprint for the 30s sync skip (not freshness)."""
    from autorefactor.extract.cache import repo_fingerprint

    try:
        return repo_fingerprint(root)
    except Exception:  # noqa: BLE001
        try:
            return f"{root.resolve()}:{root.stat().st_mtime_ns}"
        except OSError:
            return str(root)


def ensure_remote_workspace(*, path: str | None = None, force: bool = False) -> dict[str, Any]:
    """Pack local workspace and POST to remote /v1/workspace/sync."""
    global _last_sync_fingerprint, _last_sync_at, _last_sync_root
    base = remote_base_url()
    if not base:
        raise RuntimeError("AUTOREFACTOR_REMOTE_URL is not set")
    from autorefactor.mcp.engine_recovery import ensure_remote_engine

    if not ensure_remote_engine():
        raise RuntimeError("remote engine unreachable (health check failed after auto-restart)")
    started = time.time()
    root = resolve_sync_root(path)
    _last_sync_root = root
    remote_ws = _remote_workspace_for_local(root)
    if _is_product_checkout(root):
        meta = _sync_skipped_meta(root, reason="product_repo_mounted")
        meta["local_root"] = str(root)
        meta["remote_workspace"] = remote_ws
        meta["duration_ms"] = int((time.time() - started) * 1000)
        return meta
    fp = _fingerprint(root)
    now = time.time()
    if (
        not force
        and _last_sync_fingerprint == fp
        and (now - _last_sync_at) < 30
    ):
        # Unchanged tree — keep the skip window fresh so bursty queue ops
        # (confirm/mark loops) don't re-upload on a stale 30s boundary.
        _last_sync_at = now
        return {
            "ok": True,
            "skipped": True,
            "fingerprint": fp,
            "root": str(root),
            "local_root": str(root),
            "remote_workspace": remote_ws,
            "reason": "unchanged_recent",
            "duration_ms": int((time.time() - started) * 1000),
        }
    sync_url = f"{_api_base(base)}/v1/workspace/sync"
    from autorefactor.paths import load_sync_meta

    existing = load_sync_meta(root) or {}
    label = str(existing.get("label") or "").strip()
    if not label or label.lower() in {"active", "workspace", "default", "mounted"}:
        label = root.name

    def _upload() -> dict[str, Any]:
        return sync_workspace_http(
            root,
            url=sync_url,
            label=label,
            source=str(existing.get("source") or root),
        )

    try:
        result = _upload()
    except Exception:
        global _session_id
        _session_id = None
        if not ensure_remote_engine():
            raise
        result = _upload()
    _last_sync_fingerprint = fp
    _last_sync_at = now
    result["local_root"] = str(root)
    result["remote_workspace"] = remote_ws
    result["duration_ms"] = int((time.time() - started) * 1000)
    result.setdefault("label", label)
    result.setdefault("source", str(root))
    return result


def pull_remote_workspace(*, paths: list[str] | None = None, path: str | None = None) -> dict[str, Any]:
    """Download remote active workspace into the local project root."""
    base = remote_base_url()
    if not base:
        raise RuntimeError("AUTOREFACTOR_REMOTE_URL is not set")
    root = resolve_sync_root(path) if path else (_last_sync_root or resolve_sync_root(None))
    export_url = f"{_api_base(base)}/v1/workspace/export"
    return pull_workspace_http(root, url=export_url, paths=paths)


def _should_pull_after_analyze(args: dict[str, Any], result: Any) -> bool:
    """Pull when HITL or tools wrote source files (touched_paths / docs_written)."""
    if not isinstance(result, dict):
        return False
    if int(result.get("docs_written") or 0) > 0:
        return True
    touched = result.get("touched_paths")
    if isinstance(touched, list) and touched:
        return True
    nested = result.get("result")
    if isinstance(nested, dict):
        if int(nested.get("docs_written") or 0) > 0:
            return True
        touched = nested.get("touched_paths")
        if isinstance(touched, list) and touched:
            return True
    return False


def _merge_pull(result: Any, pull: dict[str, Any]) -> Any:
    if not isinstance(result, dict):
        return {
            "ok": True,
            "result": result,
            "pulled_paths": pull.get("pulled_paths") or [],
            "docs_pulled": pull.get("docs_pulled") or 0,
        }
    out = dict(result)
    out["pulled_paths"] = pull.get("pulled_paths") or []
    out["docs_pulled"] = pull.get("docs_pulled") or 0
    return out


def _maybe_pull_write_result(args: dict[str, Any], result: Any) -> Any:
    if not _should_pull_after_analyze(args, result):
        return result
    if isinstance(result, dict) and result.get("ok") is False:
        return result
    if isinstance(result, dict) and result.get("background"):
        return result
    try:
        paths = None
        if isinstance(result, dict):
            touched = result.get("touched_paths")
            if isinstance(touched, list) and touched:
                paths = [str(p) for p in touched]
            nested = result.get("result")
            if paths is None and isinstance(nested, dict):
                touched = nested.get("touched_paths")
                if isinstance(touched, list) and touched:
                    paths = [str(p) for p in touched]
        pull = pull_remote_workspace(paths=paths)
        return _merge_pull(result, pull)
    except Exception as exc:  # noqa: BLE001
        if isinstance(result, dict):
            out = dict(result)
            out["pull_error"] = str(exc)[:500]
            return out
        return {"ok": False, "error": "pull_failed", "detail": str(exc)[:500], "result": result}


def _maybe_pull_completed_job(result: Any) -> Any:
    """Pull once when a background job wrote source files."""
    if not isinstance(result, dict):
        return result
    if result.get("status") != "done":
        return result
    job_id = str(result.get("job_id") or "")
    if job_id and job_id in _pulled_job_ids:
        return result
    nested = result.get("result") if isinstance(result.get("result"), dict) else {}
    docs = 0
    touched: list | None = None
    if isinstance(nested, dict):
        docs = int(nested.get("docs_written") or 0)
        t = nested.get("touched_paths")
        if isinstance(t, list) and t:
            touched = t
    docs = docs or int(result.get("docs_written") or 0)
    if not touched:
        t = result.get("touched_paths")
        if isinstance(t, list) and t:
            touched = t
    if docs <= 0 and not touched:
        return result
    try:
        paths = [str(p) for p in touched] if touched else None
        pull = pull_remote_workspace(paths=paths)
        if job_id:
            _pulled_job_ids.add(job_id)
        return _merge_pull(result, pull)
    except Exception as exc:  # noqa: BLE001
        out = dict(result)
        out["pull_error"] = str(exc)[:500]
        return out


def _next_id() -> int:
    global _req_id
    _req_id += 1
    return _req_id


def _parse_rpc_body(raw: str) -> Any:
    if raw.startswith("event:") or "data:" in raw[:120]:
        payloads: list[Any] = []
        for line in raw.splitlines():
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if chunk:
                    try:
                        payloads.append(json.loads(chunk))
                    except json.JSONDecodeError:
                        payloads.append(chunk)
        return payloads[-1] if payloads else raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _rpc(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    session: str | None = None,
    timeout: float = 600.0,
) -> tuple[Any, dict[str, str]]:
    base = remote_base_url()
    if not base:
        raise RuntimeError("AUTOREFACTOR_REMOTE_URL is not set")
    endpoint = _mcp_endpoint(base)
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": _next_id(), "method": method}
    if params is not None:
        body["params"] = params
    headers = dict(_HEADERS)
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"remote MCP HTTP {exc.code}: {detail}") from exc
    return _parse_rpc_body(raw), hdrs


def _ensure_session() -> str:
    global _session_id
    if _session_id:
        return _session_id
    _msg, hdrs = _rpc(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "autorefactor-proxy", "version": "0.3.2"},
        },
        timeout=10.0,
    )
    session = hdrs.get("mcp-session-id") or ""
    # notifications/initialized
    base = remote_base_url()
    assert base
    endpoint = _mcp_endpoint(base)
    headers = dict(_HEADERS)
    if session:
        headers["Mcp-Session-Id"] = session
    notify = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(notify).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except urllib.error.HTTPError:
        pass
    _session_id = session or "nosession"
    return _session_id


def _unwrap_tool_result(msg: Any) -> Any:
    if not isinstance(msg, dict):
        return msg
    if "error" in msg:
        err = msg["error"]
        if isinstance(err, dict):
            return {"ok": False, "error": err.get("message") or err}
        return {"ok": False, "error": err}
    result = msg.get("result") or msg
    content = result.get("content") if isinstance(result, dict) else None
    if content and isinstance(content, list) and content:
        text = content[0].get("text") if isinstance(content[0], dict) else content[0]
        if isinstance(text, str):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    return result


def call_remote_tool(name: str, arguments: dict[str, Any] | None = None, *, timeout: float = 600.0) -> Any:
    try:
        return _call_remote_tool_once(name, arguments, timeout=timeout)
    except (RuntimeError, TimeoutError, urllib.error.URLError) as exc:
        detail = str(exc).lower()
        if "timed out" not in detail and "remote end closed" not in detail and "http" not in detail:
            raise
        from autorefactor.mcp.engine_recovery import ensure_remote_engine

        global _session_id
        _session_id = None
        if not ensure_remote_engine():
            raise
        return _call_remote_tool_once(name, arguments, timeout=timeout)


def _call_remote_tool_once(
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    timeout: float = 600.0,
) -> Any:
    session = _ensure_session()
    sid = None if session == "nosession" else session
    msg, _ = _rpc(
        "tools/call",
        {"name": name, "arguments": arguments or {}},
        session=sid,
        timeout=timeout,
    )
    return _unwrap_tool_result(msg)


_PROXY_TOOLS = frozenset(
    {
        "analyze",
        "get_analyze_job",
        "list_jobs",
        "get_progress",
        "get_status",
        "get_report",
        "list_changesets",
        "sample_queue",
        "get_next_changeset",
        "get_changeset",
        "mark_changeset",
        "confirm_changeset",
        "describe_unit",
        "get_setup_requirements",
        "get_agent_playbook",
        "list_enrich_tasks",
        "get_next_enrich_task",
        "submit_enrich_result",
        "workspace_status",
        "prepare_workspace",
        "save_config",
        "sync_workspace",
    }
)


_BOOTSTRAP_SKIP_SYNC = frozenset(
    {
        "workspace_status",
        "get_status",
        "get_agent_playbook",
        "get_setup_requirements",
    }
)


def _timeout_for(name: str, arguments: dict[str, Any]) -> float:
    if name == "analyze" and arguments.get("background"):
        return 120.0
    if name == "analyze":
        return 1800.0
    if name in {"get_analyze_job", "get_progress", "list_jobs", "get_status"}:
        return 60.0
    if name == "get_agent_playbook":
        return 15.0
    if name == "get_setup_requirements":
        return 15.0
    if name == "prepare_workspace":
        return 300.0
    if name == "sync_workspace":
        return 600.0
    return 600.0


def _local_agent_playbook() -> dict[str, Any]:
    from autorefactor.config_store import load_merged_config
    from autorefactor.mcp.guidance import build_agent_guidance

    config = load_merged_config()
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
        "needs_setup": guidance["needs_setup"],
        "setup_hint": guidance.get("setup_hint"),
        "setup": guidance["setup"],
        "next_step": (
            "Call get_status; analyze if stale or refresh requested; else follow queue workflow."
        ),
        "local": True,
    }


def _local_setup_requirements() -> dict[str, Any]:
    from autorefactor.config_store import get_setup_requirements, load_merged_config

    config = load_merged_config()
    reqs = get_setup_requirements(config)
    return {"needs_setup": bool(reqs), "requirements": reqs, "local": True}


def proxy_tool_inprocess(name: str, arguments: dict[str, Any] | None = None) -> Any:
    """Sync dirty tree when needed, then forward the tool to the remote MCP."""
    args = dict(arguments or {})
    if name == "sync_workspace":
        try:
            sync_path = _extract_sync_path(args)
            result = ensure_remote_workspace(path=sync_path, force=True)
            result.setdefault("ok", True)
            result["synced"] = not result.get("skipped")
            result["next_step"] = (
                "Call analyze(path=<same>, background=true) or analyze without path "
                "if this was the last sync."
            )
            return result
        except SyncRootError as exc:
            return {
                "ok": False,
                "error": "sync_path_required",
                "detail": str(exc),
                "next_step": (
                    "Pass path='/absolute/path/to/project' or set AUTOREFACTOR_MCP_WORKSPACE."
                ),
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "sync_failed", "detail": str(exc)[:500]}
    if name == "prepare_workspace":
        # Git prepare runs on the remote server itself
        return call_remote_tool(name, args, timeout=_timeout_for(name, args))
    if name == "get_agent_playbook":
        return _local_agent_playbook()
    if name == "get_setup_requirements":
        # Important: resolve setup requirements on the remote server, not locally.
        # The stdio proxy runs on the host; keys/config may only exist inside the
        # container where the engine actually runs.
        return call_remote_tool(name, args, timeout=_timeout_for(name, args))
    if name in _BOOTSTRAP_SKIP_SYNC and name != "workspace_status":
        if name == "get_status":
            try:
                local_root = _resolve_proxy_local_root(args)
            except SyncRootError as exc:
                return {
                    "ok": False,
                    "error": "sync_path_required",
                    "detail": str(exc),
                    "next_step": (
                        "Call sync_workspace(path='/absolute/path/to/project') first, "
                        "or pass path= on get_status."
                    ),
                }
            args["workspace"] = _remote_workspace_for_local(
                local_root, str(args.get("workspace") or "")
            )
        return call_remote_tool(name, args, timeout=_timeout_for(name, args))
    if name in _PROXY_TOOLS and name != "workspace_status":
        try:
            local_root = _resolve_proxy_local_root(args)
        except SyncRootError as exc:
            return {
                "ok": False,
                "error": "sync_path_required",
                "detail": str(exc),
                "next_step": (
                    "Call sync_workspace(path='/absolute/path/to/project') before analyze "
                    "or pass path= on this tool."
                ),
            }
        sync_path = _extract_sync_path(args)
        try:
            sync_meta = ensure_remote_workspace(path=sync_path, force=name == "analyze")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "sync_failed", "detail": str(exc)[:500]}
        if not _is_product_checkout(local_root):
            local_id = _local_pack_identity(
                local_root, sync_meta if isinstance(sync_meta, dict) else {}
            )
            remote_id = _remote_workspace_identity()
            if remote_id is not None and _workspace_mismatch(local_id, remote_id):
                return {
                    "ok": False,
                    "error": "workspace_mismatch",
                    "local": local_id,
                    "remote": remote_id,
                }
            if remote_id is not None and _file_count_drift(local_id, remote_id):
                try:
                    ensure_remote_workspace(path=str(local_root), force=True)
                except Exception:  # noqa: BLE001
                    pass
        # Never forward host absolute paths to remote — only remote selectors.
        if _extract_sync_path(args):
            args.pop("path", None)
            ws = str(args.get("workspace") or "").strip()
            if ws and Path(ws).expanduser().is_absolute():
                args.pop("workspace", None)
        # Remote analyzes mounted bind-mount (product repo) or uploaded active copy.
        if name in {
            "analyze",
            "get_analyze_job",
            "list_jobs",
            "get_progress",
            "get_status",
            "get_report",
            "list_changesets",
            "sample_queue",
            "get_next_changeset",
            "get_changeset",
            "mark_changeset",
            "confirm_changeset",
            "describe_unit",
            "list_enrich_tasks",
            "get_next_enrich_task",
            "submit_enrich_result",
        }:
            skip_workspace = name in {"get_analyze_job", "list_jobs"} or (
                name == "get_progress" and bool(args.get("job_id"))
            )
            if not skip_workspace:
                args["workspace"] = _remote_workspace_for_local(
                    local_root, str(args.get("workspace") or "")
                )
        result = call_remote_tool(name, args, timeout=_timeout_for(name, args))
        if name == "analyze":
            if isinstance(result, dict) and not result.get("background"):
                result = dict(result)
                result["synced"] = True
                if isinstance(sync_meta, dict) and sync_meta.get("skipped"):
                    result["sync_skipped"] = True
            # Analyze never writes source; pull only after HITL confirm_changeset.
            return result
        if name == "confirm_changeset":
            return _maybe_pull_write_result(args, result)
        if name in {"get_progress", "get_analyze_job"}:
            return _maybe_pull_completed_job(result)
        return result
    return call_remote_tool(name, args, timeout=_timeout_for(name, args))


def reset_proxy_state() -> None:
    """Test helper."""
    global _session_id, _req_id, _last_sync_fingerprint, _last_sync_at, _last_sync_root, _pulled_job_ids
    _session_id = None
    _req_id = 0
    _last_sync_fingerprint = None
    _last_sync_at = 0.0
    _last_sync_root = None
    _pulled_job_ids = set()


def _proxy_inprocess_enabled() -> bool:
    return (os.environ.get("AUTOREFACTOR_PROXY_INPROCESS") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def proxy_tool(name: str, arguments: dict[str, Any] | None = None) -> Any:
    """Run proxy in a fresh subprocess so stdio MCP picks up src edits without restart."""
    if _proxy_inprocess_enabled():
        return proxy_tool_inprocess(name, arguments)
    import subprocess
    import sys

    payload = json.dumps({"name": name, "arguments": arguments or {}})
    timeout = _timeout_for(name, arguments or {}) + 60.0
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "autorefactor.mcp.proxy_worker"],
            input=payload,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "error": "proxy_timeout", "detail": str(exc)[:500]}
    if proc.returncode != 0:
        return {
            "ok": False,
            "error": "proxy_worker_failed",
            "detail": (proc.stderr or proc.stdout or "")[:500],
        }
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "proxy_bad_json", "detail": (proc.stdout or "")[:500]}


def _proxy_inprocess_enabled() -> bool:
    return (os.environ.get("AUTOREFACTOR_PROXY_INPROCESS") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def proxy_tool(name: str, arguments: dict[str, Any] | None = None) -> Any:
    """Run proxy in a fresh subprocess so stdio MCP picks up src edits without restart."""
    if _proxy_inprocess_enabled():
        return proxy_tool_inprocess(name, arguments)
    import subprocess
    import sys

    payload = json.dumps({"name": name, "arguments": arguments or {}})
    timeout = _timeout_for(name, arguments or {}) + 60.0
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "autorefactor.mcp.proxy_worker"],
            input=payload,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "error": "proxy_timeout", "detail": str(exc)[:500]}
    if proc.returncode != 0:
        return {
            "ok": False,
            "error": "proxy_worker_failed",
            "detail": (proc.stderr or proc.stdout or "")[:500],
        }
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "proxy_bad_json", "detail": (proc.stdout or "")[:500]}
