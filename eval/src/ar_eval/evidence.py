"""Sandbox snapshots and hard evidence checks for product-use feedback."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ar_eval.tool_audit import audit_mcp_tools
from autorefactor.workspace_sync import FORCE_WALK_MARKER

_SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".vscode",
        "node_modules",
        "target",
        "build",
        "dist",
        ".venv",
        "venv",
        "__pycache__",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".autorefactor-data",
        ".autorefactor-context",
        "out",
    }
)


def _iter_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if any(part in _SKIP_DIR_NAMES for part in rel.parts):
            continue
        if rel.name in {FORCE_WALK_MARKER, ".ar_eval_snapshot.json"}:
            continue
        out.append(path)
    return out


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot_workspace(workspace: Path) -> dict[str, str]:
    """Map repo-relative path → sha256 for files under workspace."""
    root = workspace.resolve()
    snap: dict[str, str] = {}
    for path in _iter_files(root):
        rel = str(path.relative_to(root)).replace("\\", "/")
        try:
            snap[rel] = file_digest(path)
        except OSError:
            continue
    return snap


def write_snapshot(workspace: Path, snap: dict[str, str] | None = None) -> Path:
    data = snap if snap is not None else snapshot_workspace(workspace)
    path = workspace / ".ar_eval_snapshot.json"
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_snapshot(workspace: Path) -> dict[str, str]:
    path = workspace / ".ar_eval_snapshot.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def changed_files(workspace: Path, snap: dict[str, str] | None = None) -> list[str]:
    """Paths whose content differs from snapshot (or are new)."""
    root = workspace.resolve()
    base = snap if snap is not None else load_snapshot(workspace)
    now = snapshot_workspace(root)
    changed: list[str] = []
    for rel, dig in now.items():
        if base.get(rel) != dig:
            changed.append(rel)
    for rel in base:
        if rel not in now:
            changed.append(rel)
    return sorted(set(changed))


def verify_lifecycle_evidence(
    feedback: dict[str, Any],
    *,
    workspace: Path,
) -> dict[str, Any]:
    """
    Hard checks that the simulated user actually exercised the product.

    Returns ``{ok, reasons, changed_files, checks}``.
    """
    reasons: list[str] = []
    checks: dict[str, Any] = {}
    lifecycle = feedback.get("lifecycle") if isinstance(feedback.get("lifecycle"), dict) else {}
    source = str(feedback.get("source") or "").lower()
    checks["source"] = source
    if source != "product_use":
        reasons.append("source_must_be_product_use")

    report_reviewed = bool(lifecycle.get("report_reviewed"))
    checks["report_reviewed"] = report_reviewed
    if not report_reviewed:
        reasons.append("lifecycle.report_reviewed_required")

    applied = int(lifecycle.get("auto_proceed_applied") or 0) + int(
        lifecycle.get("hitl_proceeded") or 0
    )
    hitl_sampled = int(lifecycle.get("hitl_sampled") or 0)
    sampled_ids = [
        str(x) for x in (lifecycle.get("queue_sampled_ids") or []) if x
    ]
    finding_cs_ids: list[str] = []
    findings = feedback.get("findings") if isinstance(feedback.get("findings"), list) else []
    for f in findings:
        if not isinstance(f, dict):
            continue
        for ev in f.get("evidence") or []:
            if isinstance(ev, dict) and ev.get("changeset_id"):
                finding_cs_ids.append(str(ev.get("changeset_id")))
    checks["applied"] = applied
    checks["hitl_sampled"] = hitl_sampled
    checks["queue_sampled_ids"] = sampled_ids
    if hitl_sampled < 1 and not sampled_ids and not finding_cs_ids:
        reasons.append("must_sample_queue")

    claimed_edits = [
        str(p).replace("\\", "/").lstrip("./")
        for p in (lifecycle.get("subject_files_edited") or [])
        if p
    ]
    actual = changed_files(workspace)
    # Ignore snapshot file itself if present in lists
    actual = [p for p in actual if not p.endswith(".ar_eval_snapshot.json")]
    checks["claimed_edits"] = claimed_edits
    checks["actual_changed_files"] = actual[:50]
    checks["actual_changed_count"] = len(actual)
    reads = [
        str(p).replace("\\", "/").lstrip("./")
        for p in (lifecycle.get("subject_files_read") or [])
        if p
    ]
    checks["subject_files_read"] = reads

    if actual:
        reasons.append("must_not_edit_subject")
    if claimed_edits:
        reasons.append("must_not_claim_subject_edits")
    product_ev = 0
    for f in findings:
        if not isinstance(f, dict):
            continue
        if f.get("product_surface") or f.get("kind"):
            product_ev += 1
        for ev in f.get("evidence") or []:
            if isinstance(ev, dict) and (ev.get("changeset_id") or ev.get("sensor")):
                product_ev += 1
    checks["product_evidence_hits"] = product_ev
    if product_ev < 1 and findings:
        reasons.append("findings_lack_product_evidence")
    if not findings:
        reasons.append("findings_empty")

    tool_audit = audit_mcp_tools(feedback)
    checks["tool_audit"] = tool_audit.get("checks")
    if not tool_audit.get("ok"):
        reasons.extend(str(r) for r in (tool_audit.get("reasons") or []))

    ok = len(reasons) == 0
    return {
        "ok": ok,
        "reasons": reasons,
        "changed_files": actual,
        "checks": checks,
        "tool_audit": tool_audit,
    }
