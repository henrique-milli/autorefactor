from __future__ import annotations

from typing import Any

from autorefactor.changesets.models import Changeset, ChangesetStatus
from autorefactor.queue import load_queue, save_queue


def _clamp_confidence(value: float) -> float:
    return max(0.05, min(0.99, float(value)))


def _match_indices(
    items: list[Changeset],
    *,
    finding_ids: list[str],
    unit_ids: list[str],
) -> list[int]:
    fids = {str(x) for x in finding_ids if x}
    uids = {str(x) for x in unit_ids if x}
    matched: list[int] = []
    for idx, cs in enumerate(items):
        if cs.status not in {ChangesetStatus.PENDING, ChangesetStatus.IN_PROGRESS}:
            continue
        refs = {str(x) for x in (cs.evidence_refs or []) if x}
        if fids and refs & fids:
            matched.append(idx)
            continue
        if not fids and uids:
            target_uids = {str(t.unit_id) for t in cs.targets if t.unit_id}
            if target_uids & uids:
                matched.append(idx)
    return matched


def _cascade_skip_followups(
    items: list[Changeset],
    skipped_ids: set[str],
) -> list[str]:
    """Skip pending follow-ups whose parent was skipped. Returns newly skipped ids."""
    newly: list[str] = []
    changed = True
    while changed:
        changed = False
        for idx, cs in enumerate(items):
            if cs.status not in {ChangesetStatus.PENDING, ChangesetStatus.IN_PROGRESS}:
                continue
            parent = str((cs.metrics_delta or {}).get("parent_changeset_id") or "")
            if not parent or parent not in skipped_ids:
                continue
            items[idx] = cs.model_copy(
                update={
                    "status": ChangesetStatus.SKIPPED,
                    "notes": f"plan_enrich_rejected: parent_skipped:{parent}",
                }
            )
            skipped_ids.add(cs.id)
            newly.append(cs.id)
            changed = True
    return newly


def apply_plan_enrich_to_queue(
    out_dir,  # noqa: ANN001
    decisions: list[dict[str, Any]] | list[Any],
) -> dict[str, Any]:
    """Apply plan enrich decisions onto the changeset queue.

    - valid=false → skip matching pending/in_progress changesets
    - valid=true → update action/rationale/brief/confidence/title
    - cascade-skip follow-ups when parent is skipped
    """
    items = load_queue(out_dir)
    skipped: list[str] = []
    updated: list[str] = []
    unmatched: list[str] = []
    skipped_ids: set[str] = set()

    for raw in decisions or []:
        if hasattr(raw, "model_dump"):
            decision = raw.model_dump(mode="json")
        else:
            decision = dict(raw)
        finding_ids = [str(x) for x in (decision.get("finding_ids") or [])]
        unit_ids = [str(x) for x in (decision.get("unit_ids") or [])]
        title = str(decision.get("title") or "decision")
        idxs = _match_indices(items, finding_ids=finding_ids, unit_ids=unit_ids)
        if not idxs:
            unmatched.append(title)
            continue
        valid = bool(decision.get("valid", True))
        if not valid:
            rationale = str(decision.get("rationale") or "").strip() or "rejected"
            note = f"plan_enrich_rejected: {rationale}"[:500]
            for idx in idxs:
                cs = items[idx]
                if cs.id in skipped_ids:
                    continue
                items[idx] = cs.model_copy(
                    update={"status": ChangesetStatus.SKIPPED, "notes": note}
                )
                skipped.append(cs.id)
                skipped_ids.add(cs.id)
            continue
        action = str(decision.get("action") or "").strip()
        rationale = str(decision.get("rationale") or "").strip()
        conf_raw = decision.get("confidence")
        conf = _clamp_confidence(float(conf_raw) if conf_raw is not None else 0.5)
        new_title = str(decision.get("title") or "").strip()
        for idx in idxs:
            cs = items[idx]
            patch: dict[str, Any] = {"confidence": conf}
            if action:
                patch["action"] = action
            if rationale:
                patch["rationale"] = rationale
                patch["brief"] = rationale
            if new_title:
                patch["title"] = new_title
            items[idx] = cs.model_copy(update=patch)
            if cs.id not in updated:
                updated.append(cs.id)

    cascaded = _cascade_skip_followups(items, skipped_ids)
    for cid in cascaded:
        if cid not in skipped:
            skipped.append(cid)

    save_queue(out_dir, items)
    return {
        "skipped": skipped,
        "updated": updated,
        "unmatched": unmatched,
    }
