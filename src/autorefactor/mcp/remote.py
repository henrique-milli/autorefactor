from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from autorefactor.sync_client import pull_workspace_http, sync_workspace_http
from autorefactor.workspace_sync import mounted_workspace_dir

_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

_session_id: str | None = None
_req_id = 0
_last_sync_fingerprint: str | None = None
_last_sync_at: float = 0.0
_pulled_job_ids: set[str] = set()


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


def _autorefactor_repo_root(path: Path) -> Path | None:
    cur = path.resolve()
    if (cur / "src" / "autorefactor").is_dir() and (cur / "pyproject.toml").is_file():
        return cur
    return None


def _single_eval_subject(repo_root: Path) -> Path | None:
    subjects = repo_root / "eval" / "subjects"
    if not subjects.is_dir():
        return None
    episodes = [
        p.resolve()
        for p in subjects.iterdir()
        if p.is_dir() and _is_eval_subject_dir(p)
    ]
    if len(episodes) == 1:
        return episodes[0]
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


def _local_workspace_root() -> Path:
    env = (os.environ.get("AUTOREFACTOR_MCP_WORKSPACE") or "").strip()
    if env:
        path = Path(env).expanduser()
        if path.is_dir():
            return path.resolve()
    cwd = Path.cwd().resolve()
    discovered = _discover_eval_subject(cwd)
    if discovered is not None:
        return discovered
    repo = _autorefactor_repo_root(cwd)
    if repo is not None:
        single = _single_eval_subject(repo)
        if single is not None:
            return single
    mounted = mounted_workspace_dir()
    if mounted.is_dir() and any(mounted.iterdir()):
        return mounted.resolve()
    return cwd


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
        status = call_remote_tool("workspace_status", {})
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
    """Content fingerprint so nested edits invalidate the 30s sync skip."""
    from autorefactor.freshness import workspace_content_fingerprint

    try:
        return workspace_content_fingerprint(root)
    except Exception:  # noqa: BLE001
        try:
            return f"{root.resolve()}:{root.stat().st_mtime_ns}"
        except OSError:
            return str(root)


def ensure_remote_workspace(*, force: bool = False) -> dict[str, Any]:
    """Pack local workspace and POST to remote /v1/workspace/sync."""
    global _last_sync_fingerprint, _last_sync_at
    base = remote_base_url()
    if not base:
        raise RuntimeError("AUTOREFACTOR_REMOTE_URL is not set")
    root = _local_workspace_root()
    if _is_product_checkout(root):
        return {
            "ok": True,
            "skipped": True,
            "reason": "product_repo",
            "root": str(root),
        }
    fp = _fingerprint(root)
    now = time.time()
    if (
        not force
        and _last_sync_fingerprint == fp
        and (now - _last_sync_at) < 30
    ):
        return {"ok": True, "skipped": True, "fingerprint": fp, "root": str(root)}
    sync_url = f"{_api_base(base)}/v1/workspace/sync"
    from autorefactor.paths import load_sync_meta

    existing = load_sync_meta(root) or {}
    label = str(existing.get("label") or "").strip()
    if not label or label.lower() in {"active", "workspace", "default", "mounted"}:
        label = root.name
    result = sync_workspace_http(
        root,
        url=sync_url,
        label=label,
        source=str(existing.get("source") or root),
    )
    _last_sync_fingerprint = fp
    _last_sync_at = now
    return result


def pull_remote_workspace(*, paths: list[str] | None = None) -> dict[str, Any]:
    """Download remote active workspace into the local project root."""
    base = remote_base_url()
    if not base:
        raise RuntimeError("AUTOREFACTOR_REMOTE_URL is not set")
    root = _local_workspace_root()
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
        with urllib.request.urlopen(req, timeout=30) as resp:
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


def _timeout_for(name: str, arguments: dict[str, Any]) -> float:
    if name == "analyze" and arguments.get("background"):
        return 120.0
    if name == "analyze":
        return 1800.0
    if name in {"get_analyze_job", "get_progress", "list_jobs", "get_status"}:
        return 60.0
    if name == "prepare_workspace":
        return 300.0
    if name == "sync_workspace":
        return 600.0
    return 600.0


def proxy_tool(name: str, arguments: dict[str, Any] | None = None) -> Any:
    """Sync dirty tree when needed, then forward the tool to the remote MCP."""
    args = dict(arguments or {})
    if name == "sync_workspace":
        try:
            result = ensure_remote_workspace(force=True)
            result.setdefault("ok", True)
            result["synced"] = True
            result["next_step"] = "Call analyze (workspace is set automatically)."
            return result
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "sync_failed", "detail": str(exc)[:500]}
    if name == "prepare_workspace":
        # Git prepare runs on the remote server itself
        return call_remote_tool(name, args, timeout=_timeout_for(name, args))
    if name in _PROXY_TOOLS and name != "workspace_status":
        try:
            sync_meta = ensure_remote_workspace()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "sync_failed", "detail": str(exc)[:500]}
        local_root = _local_workspace_root()
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
                ensure_remote_workspace(force=True)
            except Exception:  # noqa: BLE001
                pass
        # Remote analyzes the uploaded active copy
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
            "get_setup_requirements",
            "get_agent_playbook",
            "list_enrich_tasks",
            "get_next_enrich_task",
            "submit_enrich_result",
        }:
            skip_workspace = name in {"get_analyze_job", "list_jobs"} or (
                name == "get_progress" and bool(args.get("job_id"))
            )
            if not skip_workspace:
                args.setdefault("workspace", "active")
                if args.get("workspace") in {None, "", "default", "mounted", "."}:
                    args["workspace"] = "active"
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
    global _session_id, _req_id, _last_sync_fingerprint, _last_sync_at, _pulled_job_ids
    _session_id = None
    _req_id = 0
    _last_sync_fingerprint = None
    _last_sync_at = 0.0
    _pulled_job_ids = set()
