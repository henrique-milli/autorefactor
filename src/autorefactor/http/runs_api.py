"""REST handlers for the Next.js report UI (runs, config, analyze)."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from autorefactor.config_store import load_merged_config, save_config
from autorefactor.paths import autorefactor_home, default_run_dir, load_run_identity, run_identity_for_workspace

_SECRET_KEYS = frozenset(
    {
        "api_key",
        "llm_api_key",
        "embed_api_key",
        "agent_api_key",
    }
)

# Optional Settings templates (fill axes — not a preset enum)
RUNTIME_TEMPLATES = [
    {
        "id": "fast_cloud",
        "label": "Fast cloud",
        "description": "Cloud chat + cloud embeddings (paste an API key)",
        "chat_backend": "http",
        "embed_backend": "http",
        "embed_device": "cpu",
        "local_chat": False,
        "describe_mode": "raw",
    },
    {
        "id": "local_agent",
        "label": "Local agent",
        "description": "Host IDE chat; local Ollama embeddings (run compose --profile ollama)",
        "chat_backend": "agent",
        "embed_backend": "local",
        "embed_device": "cpu",
        "local_chat": False,
        "describe_mode": "raw",
    },
    {
        "id": "fully_local",
        "label": "Fully local chat",
        "description": "Ollama chat + local embeddings (needs GPU-friendly host for chat model)",
        "chat_backend": "http",
        "embed_backend": "local",
        "embed_device": "cpu",
        "local_chat": True,
        "describe_mode": "raw",
    },
]

_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def runs_root() -> Path:
    return autorefactor_home() / "runs"


def resolve_run_path(slug: str) -> Path | None:
    """Safe ``runs/<slug>`` path (may not exist yet). None if slug is invalid."""
    if not slug or not _SLUG_RE.match(slug) or ".." in slug:
        return None
    path = (runs_root() / slug).resolve()
    root = runs_root().resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path


def resolve_run_dir(slug: str) -> Path | None:
    path = resolve_run_path(slug)
    if path is None or not path.is_dir():
        return None
    return path


def _read_json(path: Path) -> Any | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _changeset_count(out_dir: Path) -> int:
    for rel in ("queue/changesets.json", "changesets/queue.json"):
        data = _read_json(out_dir / rel)
        if isinstance(data, list):
            return len(data)
        if isinstance(data, dict) and isinstance(data.get("changesets"), list):
            return len(data["changesets"])
    return 0


def _run_matches_active(out_dir: Path, identity: dict[str, Any], active_ws: Path) -> bool:
    """True when this run is the one tied to the current synced workspace."""
    try:
        active_out = default_run_dir(active_ws, create=False).resolve()
        if out_dir.resolve() == active_out:
            return True
    except OSError:
        pass
    active_id = run_identity_for_workspace(active_ws)
    if out_dir.name == active_id.get("slug"):
        return True
    src = identity.get("source")
    if src and src == active_id.get("source"):
        return True
    label = identity.get("label") or identity.get("display_name")
    if label and label == active_id.get("label"):
        return True
    return False


def _freshness_for_list_row(out_dir: Path, identity: dict[str, Any]) -> dict[str, Any]:
    """Attach freshness only for the active synced workspace's run."""
    from autorefactor.freshness import active_workspace_for_freshness, freshness_payload

    active = active_workspace_for_freshness()
    if active is None or not _run_matches_active(out_dir, identity, active):
        return {}
    return freshness_payload(workspace=active, out_dir=out_dir)


def _freshness_for_summary(out_dir: Path, identity: dict[str, Any]) -> dict[str, Any]:
    from autorefactor.freshness import active_workspace_for_freshness, freshness_payload

    active = active_workspace_for_freshness()
    if active is not None and _run_matches_active(out_dir, identity, active):
        return freshness_payload(workspace=active, out_dir=out_dir)
    ws_raw = identity.get("workspace")
    if isinstance(ws_raw, str) and ws_raw.strip():
        ws = Path(ws_raw).expanduser()
        if ws.is_dir():
            return freshness_payload(workspace=ws, out_dir=out_dir)
    if active is not None:
        return freshness_payload(workspace=active, out_dir=out_dir)
    return {}


def list_runs() -> list[dict[str, Any]]:
    root = runs_root()
    if not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for child in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not child.is_dir():
            continue
        metrics = _read_json(child / "report" / "metrics.json") or {}
        exact = metrics.get("exact") or {}
        roi = metrics.get("roi") or {}
        debt = (roi.get("debt_score") or {}).get("value")
        friction = (roi.get("agent_friction") or {}).get("value")
        mtime = child.stat().st_mtime
        identity = load_run_identity(child)
        repos = exact.get("repos_scanned") or []
        if not isinstance(repos, list):
            repos = []
        row: dict[str, Any] = {
            "slug": child.name,
            "display_name": identity.get("display_name") or identity.get("label") or child.name,
            "label": identity.get("label") or child.name,
            "group": identity.get("group") or "ungrouped",
            "source": identity.get("source"),
            "repos": repos,
            "path": str(child),
            "mtime": mtime,
            "has_report": (child / "report" / "metrics.json").is_file(),
            "has_projection": (child / "report" / "projection.json").is_file(),
            "changeset_count": _changeset_count(child),
            "debt_score": debt,
            "agent_friction": friction,
        }
        row.update(_freshness_for_list_row(child, identity))
        rows.append(row)
    return rows


def run_summary(slug: str) -> dict[str, Any] | None:
    out_dir = resolve_run_dir(slug)
    if out_dir is None:
        return None
    metrics = _read_json(out_dir / "report" / "metrics.json") or {}
    exact = metrics.get("exact") or {}
    roi = metrics.get("roi") or {}
    identity = load_run_identity(out_dir)
    repos = exact.get("repos_scanned") or []
    if not isinstance(repos, list):
        repos = []
    payload: dict[str, Any] = {
        "ok": True,
        "slug": slug,
        "display_name": identity.get("display_name") or identity.get("label") or slug,
        "label": identity.get("label") or slug,
        "group": identity.get("group") or "ungrouped",
        "source": identity.get("source"),
        "repos": repos,
        "path": str(out_dir),
        "has_report": (out_dir / "report" / "metrics.json").is_file(),
        "has_projection": (out_dir / "report" / "projection.json").is_file(),
        "has_graph": (out_dir / "report" / "graph.json").is_file(),
        "changeset_count": _changeset_count(out_dir),
        "unit_count": exact.get("unit_count"),
        "debt_score": (roi.get("debt_score") or {}).get("value"),
        "agent_friction": (roi.get("agent_friction") or {}).get("value"),
        "mtime": out_dir.stat().st_mtime,
    }
    payload.update(_freshness_for_summary(out_dir, identity))
    return payload


def delete_run(slug: str) -> dict[str, Any]:
    """Delete ``~/.autorefactor/runs/<slug>/`` and related job files. Never touches workspaces.

    If the run directory is already gone, still purges orphaned job files for the slug.
    """
    from autorefactor.jobs import delete_jobs_for_run
    from autorefactor.workspace_sync import active_workspace_dir, workspaces_root

    out_dir = resolve_run_path(slug)
    if out_dir is None:
        return {"ok": False, "error": "not_found", "slug": slug}

    # Safety: refuse anything under workspaces/
    try:
        out_dir.resolve().relative_to(workspaces_root().resolve())
        return {"ok": False, "error": "refused_workspace_path", "slug": slug}
    except ValueError:
        pass
    try:
        if out_dir.resolve() == active_workspace_dir().resolve():
            return {"ok": False, "error": "refused_active_workspace", "slug": slug}
    except OSError:
        pass

    cleanup = delete_jobs_for_run(out_dir, slug)
    deleted_jobs = list(cleanup.get("deleted_jobs") or [])
    deleted_tmp = list(cleanup.get("deleted_tmp") or [])

    deleted_dir = False
    if out_dir.is_dir():
        shutil.rmtree(out_dir)
        deleted_dir = True
    elif not deleted_jobs and not deleted_tmp:
        return {"ok": False, "error": "not_found", "slug": slug}

    return {
        "ok": True,
        "deleted_run": slug,
        "deleted_dir": deleted_dir,
        "deleted_jobs": deleted_jobs,
        "deleted_tmp": deleted_tmp,
    }


def load_run_json(slug: str, *parts: str) -> Any | None:
    out_dir = resolve_run_dir(slug)
    if out_dir is None:
        return None
    return _read_json(out_dir.joinpath(*parts))


def load_run_text(slug: str, *parts: str) -> str | None:
    out_dir = resolve_run_dir(slug)
    if out_dir is None:
        return None
    path = out_dir.joinpath(*parts)
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def load_changesets(slug: str) -> list[Any] | None:
    out_dir = resolve_run_dir(slug)
    if out_dir is None:
        return None
    for rel in ("queue/changesets.json", "changesets/queue.json"):
        data = _read_json(out_dir / rel)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("changesets"), list):
            return data["changesets"]
    return []


def load_metrics_with_progress(slug: str) -> dict[str, Any] | None:
    """``report/metrics.json`` plus live ``progress`` from queue statuses."""
    from autorefactor.report.progress import compute_queue_progress

    data = load_run_json(slug, "report", "metrics.json")
    if not isinstance(data, dict):
        return None
    changesets = load_changesets(slug) or []
    out = dict(data)
    out["progress"] = compute_queue_progress(data, changesets)
    return out


def load_units_light(slug: str) -> list[dict[str, Any]] | None:
    out_dir = resolve_run_dir(slug)
    if out_dir is None:
        return None
    for rel in ("analyze/units.json", "index/units.json", "units.json"):
        data = _read_json(out_dir / rel)
        if data is None:
            continue
        units = data if isinstance(data, list) else data.get("units")
        if not isinstance(units, list):
            continue
        light: list[dict[str, Any]] = []
        for u in units:
            if not isinstance(u, dict):
                continue
            span = u.get("span") or {}
            light.append(
                {
                    "id": u.get("id"),
                    "repo": u.get("repo"),
                    "path": u.get("path"),
                    "kind": u.get("kind"),
                    "qualified_name": u.get("qualified_name"),
                    "span": span,
                }
            )
        return light
    return []


def visual_path(slug: str, name: str) -> Path | None:
    if not name or "/" in name or ".." in name or not name.endswith(".png"):
        return None
    out_dir = resolve_run_dir(slug)
    if out_dir is None:
        return None
    path = (out_dir / "report" / "visuals" / name).resolve()
    try:
        path.relative_to((out_dir / "report" / "visuals").resolve())
    except ValueError:
        return None
    return path if path.is_file() else None


def redact_config(cfg: Any) -> dict[str, Any]:
    data = cfg.model_dump(mode="json") if hasattr(cfg, "model_dump") else dict(cfg)
    out: dict[str, Any] = {}
    for key, val in data.items():
        if key in _SECRET_KEYS:
            out[key] = "set" if val else "unset"
        elif isinstance(val, Path):
            out[key] = str(val)
        else:
            out[key] = val
    if hasattr(cfg, "resolved_chat_axis"):
        out["resolved_chat_backend"] = cfg.resolved_chat_backend()
        out["resolved_embed_backend"] = cfg.resolved_embed_axis()
        out["resolved_embed_device"] = cfg.resolved_embed_device()
        out["uses_local_chat"] = cfg.uses_local_chat()
        out["ollama_required"] = cfg.ollama_required()
    return out


def get_config_payload() -> dict[str, Any]:
    cfg = load_merged_config()
    return {"ok": True, "config": redact_config(cfg)}


def patch_config(partial: dict[str, Any], *, user_level: bool = True) -> dict[str, Any]:
    clean = {k: v for k, v in partial.items() if v not in {"set", "unset"}}
    for key in list(clean):
        if key in _SECRET_KEYS and clean[key] in ("", None):
            clean.pop(key)
    clean.pop("preset", None)
    path = save_config(clean, user_level=user_level)
    cfg = load_merged_config()
    return {"ok": True, "path": str(path), "config": redact_config(cfg)}


def list_templates() -> dict[str, Any]:
    """Return axis templates for the Settings UI."""
    items = [
        {
            "id": t["id"],
            "label": t["label"],
            "description": t["description"],
            "chat_backend": t["chat_backend"],
            "embed_backend": t["embed_backend"],
            "embed_device": t["embed_device"],
            "local_chat": t["local_chat"],
            "describe_mode": t["describe_mode"],
        }
        for t in RUNTIME_TEMPLATES
    ]
    return {"ok": True, "templates": items}


def cors_headers(origin: str | None = None, *, allow_mutations: bool = False) -> dict[str, str]:
    """CORS for browser access. Only allowlist local/dev web origins (never reflect arbitrary Origin)."""
    allowed = {
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3001",
    }
    # Env override: comma-separated extra origins
    import os

    extra = (os.environ.get("AUTOREFACTOR_CORS_ORIGINS") or "").strip()
    if extra:
        allowed.update(o.strip() for o in extra.split(",") if o.strip())

    if origin and origin in allowed:
        allow = origin
    else:
        # Prefer first allowlisted origin for same-origin Next proxy (no Origin header)
        allow = "http://localhost:3000"

    methods = "GET, OPTIONS" if not allow_mutations else "GET, POST, PATCH, DELETE, OPTIONS"
    return {
        "Access-Control-Allow-Origin": allow,
        "Access-Control-Allow-Methods": methods,
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
        "Access-Control-Max-Age": "86400",
        "Vary": "Origin",
    }
