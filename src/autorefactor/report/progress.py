"""Queue progress vs analyze-time baseline (estimated realized / remaining)."""

from __future__ import annotations

from typing import Any

from autorefactor.changesets.models import Changeset, ChangesetStatus
from autorefactor.report.roi import (
    agent_friction_score,
    composite_debt_score,
    compute_debt_components,
)

_OPEN = frozenset({ChangesetStatus.PENDING, ChangesetStatus.IN_PROGRESS})
_DEDUP = frozenset({"merge", "extract_shared", "delete_duplicate"})


def _as_changesets(raw: list[Any]) -> list[Changeset]:
    out: list[Changeset] = []
    for item in raw:
        if isinstance(item, Changeset):
            out.append(item)
            continue
        if isinstance(item, dict):
            try:
                out.append(Changeset.model_validate(item))
            except Exception:  # noqa: BLE001
                continue
    return out


def _sum_delta(items: list[Changeset], key: str) -> int:
    return sum(int(cs.metrics_delta.get(key) or 0) for cs in items)


def _bucket(items: list[Changeset]) -> dict[str, Any]:
    return {
        "count": len(items),
        "estimated_loc_removed": _sum_delta(items, "estimated_loc_removed"),
        "loc_under_review": _sum_delta(items, "loc_under_review"),
        "high_confidence": sum(1 for cs in items if cs.confidence >= 0.85),
        "dedup": sum(1 for cs in items if cs.action in _DEDUP),
        "split": sum(1 for cs in items if cs.action == "split"),
        "ensure_tests": sum(1 for cs in items if cs.action == "ensure_tests"),
        "document": sum(1 for cs in items if cs.action == "document"),
    }


def _debt_friction_for_queue(
    items: list[Changeset],
    *,
    metrics: dict[str, Any],
) -> tuple[float | None, float | None, dict[str, float]]:
    """Recompute debt/friction pressure from a changeset subset + frozen finding counts."""
    exact = metrics.get("exact") or {}
    flags = exact.get("flags_by_goal") or {}
    if not isinstance(flags, dict):
        flags = {}
    findings_structure = int(flags.get("structure") or 0) + int(flags.get("static") or 0)
    findings_clone = (
        int(flags.get("method_similarity") or 0)
        + int(flags.get("class_similarity") or 0)
        + int(flags.get("structural_clone") or 0)
        + int(flags.get("slop") or 0)
    )
    clone_paths = int((metrics.get("impact") or {}).get("clone_paths_at_risk") or 0)
    slop_score = float(exact.get("slop_score") or (metrics.get("impact") or {}).get("slop_score") or 0)
    split_cs = sum(1 for cs in items if cs.action == "split")
    test_cs = sum(1 for cs in items if cs.action == "ensure_tests")
    doc_cs = sum(1 for cs in items if cs.action == "document")
    # Clone pressure from remaining dedup actions when subset is non-empty
    dedup_cs = sum(1 for cs in items if cs.action in _DEDUP)
    components = compute_debt_components(
        split_cs=split_cs,
        clone_paths=min(clone_paths, max(dedup_cs * 2, clone_paths if items else 0)),
        test_cs=test_cs,
        doc_cs=doc_cs,
        slop_score=slop_score if items else 0.0,
        queue_n=len(items),
        findings_structure=findings_structure if items else 0,
        findings_clone=findings_clone if items else 0,
    )
    if not items:
        # Queue cleared — residual finding pressure only from frozen findings is misleading;
        # treat remaining debt/friction as 0 for storytelling once everything is done/skipped.
        zero = {k: 0.0 for k in ("complexity", "clone", "coverage", "docs", "slop")}
        return 0.0, 0.0, zero
    debt = composite_debt_score(components)
    friction = agent_friction_score(
        debt_components=components,
        clone_paths=clone_paths if dedup_cs or findings_clone else 0,
        split_cs=split_cs,
        test_cs=test_cs,
    )
    return debt, friction, components


def compute_queue_progress(
    metrics: dict[str, Any] | None,
    changesets: list[Any] | None,
) -> dict[str, Any]:
    """Compare analyze-time metrics to live queue status.

    * ``at_analyze`` — frozen scores from ``metrics.json``
    * ``realized`` — sum of ``metrics_delta`` on ``done`` items (+ debt/friction drop est.)
    * ``remaining`` — open queue (pending/in_progress) estimates
    """
    metrics = metrics if isinstance(metrics, dict) else {}
    items = _as_changesets(list(changesets or []))
    roi = metrics.get("roi") or {}
    impact = metrics.get("impact") or {}

    baseline_debt = (roi.get("debt_score") or {}).get("value")
    baseline_friction = (roi.get("agent_friction") or {}).get("value")
    baseline_loc = int(impact.get("estimated_loc_removed") or 0)
    baseline_review = int(impact.get("loc_under_review") or 0)
    baseline_tasks = int(
        impact.get("agent_ready_changesets")
        or (metrics.get("exact") or {}).get("changesets_with_targets")
        or len([c for c in items if c.targets])
        or len(items)
    )

    done = [c for c in items if c.status == ChangesetStatus.DONE]
    open_cs = [c for c in items if c.status in _OPEN]
    skipped = [c for c in items if c.status == ChangesetStatus.SKIPPED]
    blocked = [c for c in items if c.status == ChangesetStatus.BLOCKED]

    done_b = _bucket(done)
    open_b = _bucket(open_cs)
    skipped_b = _bucket(skipped)
    blocked_b = _bucket(blocked)

    rem_debt, rem_friction, rem_components = _debt_friction_for_queue(open_cs, metrics=metrics)

    realized_debt: float | None = None
    realized_friction: float | None = None
    if isinstance(baseline_debt, (int, float)) and rem_debt is not None:
        realized_debt = round(max(0.0, float(baseline_debt) - float(rem_debt)), 1)
    if isinstance(baseline_friction, (int, float)) and rem_friction is not None:
        realized_friction = round(max(0.0, float(baseline_friction) - float(rem_friction)), 1)

    return {
        "method": (
            "Estimates only: at_analyze from metrics.json; realized LOC from done "
            "metrics_delta; remaining debt/friction recomputed from open queue + frozen findings."
        ),
        "counts": {
            "total": len(items),
            "done": len(done),
            "open": len(open_cs),
            "skipped": len(skipped),
            "blocked": len(blocked),
        },
        "at_analyze": {
            "debt_score": baseline_debt if isinstance(baseline_debt, (int, float)) else None,
            "agent_friction": baseline_friction
            if isinstance(baseline_friction, (int, float))
            else None,
            "estimated_loc_removed": baseline_loc,
            "loc_under_review": baseline_review,
            "tasks": baseline_tasks,
            "debt_components": ((roi.get("debt_score") or {}).get("components") or None),
        },
        "realized": {
            "debt_score_drop": realized_debt,
            "agent_friction_drop": realized_friction,
            "estimated_loc_removed": done_b["estimated_loc_removed"],
            "loc_under_review": done_b["loc_under_review"],
            "tasks": done_b["count"],
            **{k: done_b[k] for k in ("high_confidence", "dedup", "split", "ensure_tests", "document")},
        },
        "remaining": {
            "debt_score": rem_debt,
            "agent_friction": rem_friction,
            "debt_components": rem_components,
            "estimated_loc_removed": open_b["estimated_loc_removed"],
            "loc_under_review": open_b["loc_under_review"],
            "tasks": open_b["count"],
            **{k: open_b[k] for k in ("high_confidence", "dedup", "split", "ensure_tests", "document")},
        },
        "skipped": skipped_b,
        "blocked": blocked_b,
    }
