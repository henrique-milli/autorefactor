from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


SYNC_META_NAME = ".autorefactor-sync.json"
_EVAL_SNAPSHOT_NAME = ".ar_eval_snapshot.json"
_GENERIC_WORKSPACE_NAMES = frozenset({"active", "workspace", "out", "app", "src"})


def autorefactor_home() -> Path:
    """User-level autorefactor data root (``~/.autorefactor``)."""
    return Path.home() / ".autorefactor"


def safe_slug_part(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip()).strip("-")
    return safe or "workspace"


def load_sync_meta(workspace: Path) -> dict[str, Any] | None:
    """Read ``.autorefactor-sync.json`` from a synced workspace root, if present."""
    path = Path(workspace).expanduser() / SYNC_META_NAME
    if not path.is_file():
        # Also check parent when cwd is nested under active
        alt = Path(workspace).expanduser().resolve() / SYNC_META_NAME
        path = alt if alt.is_file() else path
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def run_identity_for_workspace(workspace: Path) -> dict[str, Any]:
    """Human-facing identity for a workspace / sync.

    * ``label`` — display name (sync label or folder name, e.g. ``billing-service``)
    * ``group`` — parent folder of the source path (e.g. ``monorepo``)
    * ``source`` — host path or git URL when known
    * ``slug`` — ``{label}-{digest10}`` keyed by source path when absolute, else workspace
    """
    resolved = Path(workspace).expanduser().resolve()
    meta = load_sync_meta(resolved) or {}
    source_raw = meta.get("source")
    source = str(source_raw).strip() if source_raw else None
    label = str(meta.get("label") or "").strip() or None

    if not label or label.lower() in _GENERIC_WORKSPACE_NAMES:
        if source and source not in {"git", "sync"} and not source.startswith("git@"):
            try:
                label = Path(source).name or label
            except Exception:  # noqa: BLE001
                pass
        if not label or label.lower() in _GENERIC_WORKSPACE_NAMES:
            label = resolved.name or "workspace"

    group = "ungrouped"
    if source and source not in {"git", "sync"}:
        try:
            src_path = Path(source)
            parent = src_path.parent.name
            if parent and parent not in {".", ""}:
                group = safe_slug_part(parent)
        except Exception:  # noqa: BLE001
            group = "ungrouped"
    elif meta.get("kind") == "git":
        group = "git"

    # Prefer absolute host source for stable digests across active-workspace remounts
    key = source if source and (source.startswith("/") or Path(source).is_absolute()) else str(resolved)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]
    slug = f"{safe_slug_part(label)}-{digest}"
    return {
        "label": label,
        "display_name": label,
        "group": group,
        "source": source,
        "workspace": str(resolved),
        "slug": slug,
        "key": key,
    }


def workspace_run_slug(workspace: Path) -> str:
    """Stable short slug for a workspace — prefers sync label over ``active``."""
    return str(run_identity_for_workspace(workspace)["slug"])


def default_run_dir(workspace: Path, *, create: bool = True) -> Path:
    """Per-workspace artifact directory under ``~/.autorefactor/runs/``."""
    path = autorefactor_home() / "runs" / workspace_run_slug(workspace)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def eval_episode_label(workspace: Path) -> str | None:
    """Episode folder name when ``workspace`` is an eval subject (``eval/subjects/<ep>``)."""
    resolved = Path(workspace).expanduser().resolve()
    if (resolved / _EVAL_SNAPSHOT_NAME).is_file():
        return resolved.name
    parts = resolved.parts
    if "eval" in parts and "subjects" in parts:
        idx = parts.index("subjects")
        if idx > 0 and parts[idx - 1] == "eval" and idx + 1 < len(parts):
            return parts[idx + 1]
    return None


def _find_newest_episode_run_dir(episode_label: str) -> Path | None:
    """Newest ``~/.autorefactor/runs/<episode>-*`` whose identity label matches."""
    runs_root = autorefactor_home() / "runs"
    if not runs_root.is_dir():
        return None
    want = safe_slug_part(episode_label)
    candidates: list[tuple[float, Path]] = []
    for run_dir in runs_root.iterdir():
        if not run_dir.is_dir():
            continue
        ident = load_run_identity(run_dir)
        label = safe_slug_part(str(ident.get("label") or ""))
        if label != want and not run_dir.name.startswith(f"{want}-"):
            continue
        if label != want:
            continue
        try:
            mtime = run_dir.stat().st_mtime
        except OSError:
            mtime = 0.0
        candidates.append((mtime, run_dir))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0], reverse=True)
    return candidates[0][1]


def _run_label_matches(run_dir: Path, workspace: Path) -> bool:
    want = run_identity_for_workspace(workspace)
    ident = load_run_identity(run_dir)
    want_label = safe_slug_part(str(want.get("label") or ""))
    run_label = safe_slug_part(str(ident.get("label") or ""))
    if want_label and run_label == want_label:
        return True
    if run_dir.name == want.get("slug"):
        return True
    # Do not match generic sources (ar_eval/sync) — that reuses janus-bonus-* for loop-retest-*.
    return False


def _run_has_populated_queue(run_dir: Path) -> bool:
    """True when queue/changesets.json has at least one pending or done item."""
    from autorefactor.changesets.models import ChangesetStatus

    for rel in ("queue/changesets.json", "changesets/queue.json"):
        path = run_dir / rel
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        items = data.get("changesets", data if isinstance(data, list) else [])
        if not isinstance(items, list):
            continue
        for raw in items:
            if not isinstance(raw, dict):
                continue
            status = str(raw.get("status") or ChangesetStatus.PENDING.value).lower()
            if status in {ChangesetStatus.PENDING.value, ChangesetStatus.DONE.value, ChangesetStatus.IN_PROGRESS.value}:
                return True
    return False


def _find_best_run_dir(workspace: Path) -> Path | None:
    """Newest run for this workspace label that already has a populated queue."""
    runs_root = autorefactor_home() / "runs"
    if not runs_root.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for run_dir in runs_root.iterdir():
        if not run_dir.is_dir():
            continue
        if not _run_label_matches(run_dir, workspace):
            continue
        if not _run_has_populated_queue(run_dir):
            continue
        try:
            mtime = run_dir.stat().st_mtime
        except OSError:
            mtime = 0.0
        candidates.append((mtime, run_dir))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0], reverse=True)
    return candidates[0][1]


def run_has_analyze_artifacts(out_dir: Path) -> bool:
    """True when a run directory looks like analyze completed (not an empty stub)."""
    out_dir = Path(out_dir)
    markers = (
        "analyze/units.json",
        "index/units.json",
        "manifest.json",
        "report/metrics.json",
        "queue/changesets.json",
        "changesets/queue.json",
    )
    return any((out_dir / rel).is_file() for rel in markers)


def resolve_out_dir(workspace: Path, out: Path | None) -> Path:
    """Use explicit ``out`` when provided; otherwise default or eval-episode run dir."""
    if out is not None:
        return Path(out).expanduser()
    episode = eval_episode_label(workspace)
    if episode:
        episode_run = _find_newest_episode_run_dir(episode)
        if episode_run is not None:
            default = default_run_dir(workspace, create=False)
            default_label = ""
            if default.is_dir():
                default_label = safe_slug_part(
                    str(load_run_identity(default).get("label") or "")
                )
            if safe_slug_part(episode) != default_label:
                return episode_run
    populated = _find_best_run_dir(workspace)
    if populated is not None:
        default = default_run_dir(workspace, create=False)
        if not default.is_dir() or not _run_has_populated_queue(default):
            return populated
        try:
            if populated.stat().st_mtime > default.stat().st_mtime:
                return populated
        except OSError:
            return populated
    return default_run_dir(workspace)


def write_run_identity(out_dir: Path, workspace: Path) -> dict[str, Any]:
    """Persist identity next to report artifacts for the UI."""
    from autorefactor.freshness import enrich_identity_with_fingerprint

    identity = run_identity_for_workspace(workspace)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Actual directory name is the slug used for this run
    identity = {**identity, "slug": out_dir.name}
    identity = enrich_identity_with_fingerprint(identity, workspace)
    path = out_dir / "run_identity.json"
    path.write_text(json.dumps(identity, indent=2), encoding="utf-8")
    return identity


def load_run_identity(out_dir: Path) -> dict[str, Any]:
    """Load or infer display identity for an existing run directory."""
    out_dir = Path(out_dir)
    path = out_dir / "run_identity.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("label"):
                data.setdefault("display_name", data["label"])
                data.setdefault("group", "ungrouped")
                data.setdefault("slug", out_dir.name)
                return data
        except (OSError, json.JSONDecodeError):
            pass

    manifest: dict[str, Any] = {}
    man_path = out_dir / "manifest.json"
    if man_path.is_file():
        try:
            loaded = json.loads(man_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                manifest = loaded
        except (OSError, json.JSONDecodeError):
            pass

    ws = manifest.get("workspace")
    if ws:
        inferred = run_identity_for_workspace(Path(ws))
        inferred["slug"] = out_dir.name
        return inferred

    # Last resort: strip trailing -digest from folder name
    name = out_dir.name
    parts = name.rsplit("-", 1)
    label = parts[0] if len(parts) == 2 and len(parts[1]) == 10 else name
    if label.lower() in _GENERIC_WORKSPACE_NAMES:
        label = name
    return {
        "label": label,
        "display_name": label,
        "group": "ungrouped",
        "source": None,
        "workspace": ws,
        "slug": name,
    }


def resolve_repo_file(workspace: Path, repo: str, rel: str) -> Path:
    """Map ``(repo, path)`` from the index onto a filesystem path under ``workspace``.

    Multi-repo layouts store units under ``workspace/<repo>/<rel>``.
    Single-root layouts (``discover_repos`` returned the workspace itself) store
    ``repo`` as the workspace directory name (e.g. ``active``) while ``rel`` is
    already relative to that root — join only ``workspace/rel``.
    """
    workspace = workspace.expanduser().resolve()
    rel_norm = (rel or "").replace("\\", "/").lstrip("./")
    repo_name = (repo or "").strip().strip("/").replace("\\", "/")
    flat = workspace / rel_norm if rel_norm else workspace

    if not repo_name or repo_name in {".", workspace.name}:
        return flat

    nested_root = workspace / repo_name
    nested = nested_root / rel_norm if rel_norm else nested_root
    try:
        nested_resolved = nested_root.resolve()
    except OSError:
        nested_resolved = nested_root

    if nested_root.is_dir() and nested_resolved != workspace:
        if nested.exists() or not flat.exists():
            return nested
        return flat

    if nested.exists():
        return nested
    return flat
