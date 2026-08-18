"""Workspace content freshness fingerprints (sync-based; git optional)."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autorefactor.paths import SYNC_META_NAME, load_run_identity, load_sync_meta

_EVAL_SNAPSHOT_NAME = ".ar_eval_snapshot.json"

_SKIP_DIR = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "target",
    "build",
    "dist",
    ".venv",
    "venv",
    "__pycache__",
    ".autorefactor",
    ".idea",
    ".gradle",
}


def _run_has_analyze_artifacts(out_dir: Path) -> bool:
    """Compat wrapper for older environments missing `paths.run_has_analyze_artifacts`."""
    try:
        from autorefactor.paths import run_has_analyze_artifacts

        return run_has_analyze_artifacts(out_dir)
    except (ImportError, AttributeError):
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


def workspace_content_fingerprint(workspace: Path) -> str:
    """Fingerprint the synced workspace tree (paths + size + content hash).

    Uses content hashes (not mtime) so analyze/sync re-reads of unchanged files
    do not flip ``fresh=false`` / ``content_changed``. Skip rules match extract
    ``repo_fingerprint``. For multi-repo layouts (immediate child dirs that look
    like repos), hashes each child; otherwise hashes the workspace root once.
    """
    root = Path(workspace).expanduser().resolve()
    h = hashlib.sha256()
    if not root.is_dir():
        return h.hexdigest()

    children = sorted(
        p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".") and p.name != "node_modules"
    )
    # Heuristic: multi-repo if ≥2 code-ish children with manifests/src
    multi = []
    for child in children:
        if any(
            (child / name).exists()
            for name in ("pom.xml", "build.gradle", "package.json", "go.mod", "pyproject.toml", "src")
        ):
            multi.append(child)
    targets = multi if len(multi) >= 2 else [root]

    for target in targets:
        h.update(target.name.encode("utf-8", errors="replace"))
        h.update(b"\0")
        for path in sorted(target.rglob("*")):
            if not path.is_file():
                continue
            rel_parts = path.relative_to(target).parts
            if any(p in _SKIP_DIR or p.startswith(".") for p in rel_parts[:-1]):
                continue
            try:
                rel = str(path.relative_to(target)).replace("\\", "/")
                if rel == _EVAL_SNAPSHOT_NAME or rel.endswith(f"/{_EVAL_SNAPSHOT_NAME}"):
                    continue
                if rel == SYNC_META_NAME or rel.endswith(f"/{SYNC_META_NAME}"):
                    continue
                st = path.stat()
                data = path.read_bytes()
            except OSError:
                continue
            h.update(rel.encode("utf-8", errors="replace"))
            h.update(b"\0")
            h.update(str(st.st_size).encode())
            h.update(b"\0")
            h.update(hashlib.sha256(data).digest())
            h.update(b"\n")
        h.update(b"\n")
    return h.hexdigest()


def target_paths_fingerprint(workspace: Path, targets: list[Any]) -> str:
    """Hash on-disk contents for changeset target paths (repo/path pairs)."""
    from autorefactor.paths import resolve_repo_file

    root = Path(workspace).expanduser().resolve()
    h = hashlib.sha256()
    ordered = sorted(
        targets,
        key=lambda t: (
            getattr(t, "repo", None) or "",
            getattr(t, "path", None) or "",
            getattr(t, "symbol", None) or "",
        ),
    )
    for target in ordered:
        repo = getattr(target, "repo", None) or ""
        rel = getattr(target, "path", None) or ""
        h.update(repo.encode("utf-8", errors="replace"))
        h.update(b"\0")
        h.update(rel.encode("utf-8", errors="replace"))
        h.update(b"\0")
        path = resolve_repo_file(root, repo, rel)
        if path.is_file():
            try:
                h.update(path.read_bytes())
            except OSError:
                pass
        h.update(b"\n")
    return h.hexdigest()


def confirm_apply_baselines(workspace: Path, targets: list[Any]) -> dict[str, str]:
    """Snapshot workspace + target-path hashes at confirm time (post optional HITL writes)."""
    return {
        "confirm_workspace_fingerprint": workspace_content_fingerprint(workspace),
        "confirm_target_fingerprint": target_paths_fingerprint(workspace, targets),
    }


def workspace_apply_changed(
    workspace: Path,
    *,
    baseline_workspace_fp: str | None,
    baseline_target_fp: str | None,
    targets: list[Any],
) -> bool:
    """True when workspace or target paths differ from the post-confirm baseline."""
    if not baseline_workspace_fp and not baseline_target_fp:
        return True
    current_ws = workspace_content_fingerprint(workspace)
    if baseline_workspace_fp and current_ws != baseline_workspace_fp:
        return True
    if baseline_target_fp:
        current_targets = target_paths_fingerprint(workspace, targets)
        if current_targets != baseline_target_fp:
            return True
    if baseline_workspace_fp and current_ws == baseline_workspace_fp:
        return False
    return True


def advance_run_fingerprint(out_dir: Path, workspace: Path) -> dict[str, Any]:
    """Re-anchor run identity fingerprint after intentional queue apply deltas."""
    out_dir = Path(out_dir)
    identity = load_run_identity(out_dir)
    identity = enrich_identity_with_fingerprint(identity, workspace)
    path = out_dir / "run_identity.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(identity, indent=2), encoding="utf-8")
    return identity


def _queue_has_pending_apply(out_dir: Path) -> bool:
    from autorefactor.changesets.models import ChangesetStatus
    from autorefactor.queue import load_queue

    items = load_queue(out_dir)
    return any(
        c.status in {ChangesetStatus.PENDING, ChangesetStatus.IN_PROGRESS} for c in items
    )


def workspace_git_meta(workspace: Path) -> dict[str, Any]:
    """Best-effort git HEAD + dirty flag; never required for freshness."""
    root = Path(workspace).expanduser().resolve()
    git_dir = root / ".git"
    if not git_dir.exists():
        return {"available": False, "head": None, "dirty": None}
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        porcelain = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if head.returncode != 0:
            return {"available": False, "head": None, "dirty": None}
        return {
            "available": True,
            "head": (head.stdout or "").strip() or None,
            "dirty": bool((porcelain.stdout or "").strip()),
        }
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False, "head": None, "dirty": None}


def backfill_run_identity_fingerprint(out_dir: Path, workspace: Path) -> dict[str, Any] | None:
    """Persist content_fingerprint when analyze artifacts exist but identity lacks one."""
    out_dir = Path(out_dir)
    if not out_dir.is_dir() or not _run_has_analyze_artifacts(out_dir):
        return None
    identity = load_run_identity(out_dir)
    if identity.get("content_fingerprint"):
        return identity
    from autorefactor.paths import write_run_identity

    return write_run_identity(out_dir, workspace)


def freshness_payload(
    *,
    workspace: Path,
    out_dir: Path | None,
) -> dict[str, Any]:
    """Compare current workspace fingerprint to the run's stored fingerprint."""
    current = workspace_content_fingerprint(workspace)
    git = workspace_git_meta(workspace)
    if out_dir is None or not Path(out_dir).is_dir():
        return {
            "fresh": False,
            "apply_safe": False,
            "freshness": {
                "run_fingerprint": None,
                "current_fingerprint": current,
                "reason": "no_run",
                "git": git,
                "apply_safe": False,
            },
        }
    out_path = Path(out_dir)
    identity = load_run_identity(out_path)
    run_fp = identity.get("content_fingerprint")
    if not run_fp:
        backfilled = backfill_run_identity_fingerprint(out_path, workspace)
        if backfilled:
            identity = backfilled
            run_fp = identity.get("content_fingerprint")
    if not run_fp:
        return {
            "fresh": False,
            "apply_safe": False,
            "freshness": {
                "run_fingerprint": None,
                "current_fingerprint": current,
                "reason": "missing_fingerprint",
                "git": git,
                "apply_safe": False,
            },
        }
    ok = str(run_fp) == current
    reason = "ok" if ok else "content_changed"
    apply_safe = bool(not ok and reason == "content_changed" and _queue_has_pending_apply(out_path))
    return {
        "fresh": ok,
        "apply_safe": apply_safe,
        "freshness": {
            "run_fingerprint": run_fp,
            "current_fingerprint": current,
            "reason": reason,
            "git": git,
            "apply_safe": apply_safe,
        },
    }


def enrich_identity_with_fingerprint(identity: dict[str, Any], workspace: Path) -> dict[str, Any]:
    """Attach fingerprint fields used at analyze time."""
    out = dict(identity)
    out["content_fingerprint"] = workspace_content_fingerprint(workspace)
    git = workspace_git_meta(workspace)
    if git.get("available"):
        out["git_head"] = git.get("head")
        out["git_dirty"] = git.get("dirty")
    out["fingerprinted_at"] = datetime.now(timezone.utc).isoformat()
    return out


def active_workspace_for_freshness() -> Path | None:
    """Prefer synced active workspace when present."""
    from autorefactor.workspace_sync import active_workspace_dir

    active = active_workspace_dir()
    if active.is_dir() and load_sync_meta(active):
        return active
    if active.is_dir() and any(active.iterdir()):
        return active
    return None
