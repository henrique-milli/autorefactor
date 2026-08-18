"""Queue prep ordering — shared by plan follow-ups and changeset store."""

from __future__ import annotations

from autorefactor.changesets.models import Changeset

_CLONE_EXTRACT_GOALS = frozenset({"method_similarity", "structural_clone", "slop"})


def coverage_first_mode(changesets: list[Changeset]) -> bool:
    """True when any ensure_tests prep is present (primary or follow-up)."""
    return any((c.action or "").strip().lower() == "ensure_tests" for c in changesets)


_ARCHITECTURAL_WIRING_KINDS = frozenset({"orphan_kafka", "test_only_handler"})


def _changeset_text_blob(cs: Changeset) -> str:
    return " ".join(
        filter(
            None,
            [
                cs.title or "",
                cs.rationale or "",
                cs.brief or "",
                " ".join(cs.evidence_refs or []),
            ],
        )
    )


def is_semantic_extract_shared(cs: Changeset) -> bool:
    """High-value clone/dedup extract_shared (method_similarity, structural_clone, slop)."""
    action = (cs.action or "").strip().lower()
    if action != "extract_shared":
        return False
    goal = str((cs.metrics_delta or {}).get("goal") or "")
    if goal in _CLONE_EXTRACT_GOALS:
        return True
    blob = _changeset_text_blob(cs).lower()
    return (
        "sim." in blob
        or "structural clone" in blob
        or "@exceptionhandler" in blob
        or "exceptionhandler" in blob
    )


def is_threshold_band_structure_split(cs: Changeset) -> bool:
    """Length-threshold structure split (weak signal vs semantic dedups)."""
    action = (cs.action or "").strip().lower()
    if action != "split":
        return False
    goal = str((cs.metrics_delta or {}).get("goal") or "")
    if goal == "structure":
        return True
    return "lines (threshold" in _changeset_text_blob(cs).lower()


def prep_rank(cs: Changeset, *, coverage_first: bool = False) -> int:
    """Lower = earlier in queue."""
    kind = str((cs.metrics_delta or {}).get("kind") or "")
    if kind in _ARCHITECTURAL_WIRING_KINDS:
        return 0
    action = (cs.action or "").strip().lower()
    is_followup = bool(cs.metrics_delta.get("followup"))
    if action == "ensure_tests":
        return 0
    semantic_extract = is_semantic_extract_shared(cs) and float(cs.confidence) >= 0.85
    threshold_split = is_threshold_band_structure_split(cs) and not is_followup
    if coverage_first:
        if semantic_extract:
            return 1
        if not is_followup and float(cs.confidence) >= 0.85:
            return 1
        if action == "document" and not is_followup:
            return 2
        if threshold_split:
            return 4
        if not is_followup:
            return 3
        if action == "document":
            return 4
        return 5
    # Without ensure_tests prep: run structural primaries before any follow-ups;
    # document follow-ups (prep) defer until autorun structural items are ready.
    if semantic_extract:
        return 1
    if not is_followup:
        if threshold_split:
            return 3
        if action == "document":
            return 2
        return 1
    if action == "document":
        return 4
    return 3
