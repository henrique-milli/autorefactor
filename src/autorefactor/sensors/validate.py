from __future__ import annotations

from autorefactor.domain import Finding, SuggestedAction


_PAIR_ACTIONS = {
    SuggestedAction.DELETE_DUPLICATE,
    SuggestedAction.MERGE,
    SuggestedAction.MOVE,
}

_SINGLE_ACTIONS = {
    SuggestedAction.SPLIT,
    SuggestedAction.DOCUMENT,
    SuggestedAction.ENSURE_TESTS,
    SuggestedAction.EXTRACT_SHARED,
}


def structural_reason(finding: Finding, known_ids: set[str]) -> str | None:
    unique = list(dict.fromkeys(finding.unit_ids))
    if not unique:
        return "empty_targets"
    unresolved = [uid for uid in unique if uid not in known_ids]
    if unresolved:
        return "unresolved_targets"
    action = finding.action_hint
    if action in _PAIR_ACTIONS and len(unique) < 2:
        return "insufficient_targets"
    if action in _SINGLE_ACTIONS and len(unique) < 1:
        return "empty_targets"
    return None


def validate_findings(
    findings: list[Finding], known_ids: set[str]
) -> tuple[list[Finding], list[tuple[Finding, str]]]:
    kept: list[Finding] = []
    rejected: list[tuple[Finding, str]] = []
    for finding in findings:
        reason = structural_reason(finding, known_ids)
        if reason:
            rejected.append((finding, reason))
        else:
            kept.append(finding)
    return kept, rejected


def decision_structural_reason(
    decision: dict,
    known_ids: set[str],
    units_by_id: dict | None = None,
) -> str | None:
    if not decision.get("valid", True):
        return "invalid"
    action_raw = (decision.get("action") or "document").strip().lower().replace("-", "_")
    try:
        action = SuggestedAction(action_raw)
    except ValueError:
        action = SuggestedAction.DOCUMENT
    unit_ids = list(dict.fromkeys(str(x) for x in (decision.get("unit_ids") or [])))
    if not unit_ids:
        return "empty_targets"
    unresolved = [uid for uid in unit_ids if uid not in known_ids]
    if unresolved:
        return "unresolved_targets"
    if action in _PAIR_ACTIONS and len(unit_ids) < 2:
        return "insufficient_targets"
    if action in {SuggestedAction.DELETE_DUPLICATE, SuggestedAction.MERGE} and units_by_id:
        keys: set[tuple[str, str, str]] = set()
        for uid in unit_ids:
            unit = units_by_id.get(uid)
            if unit is None:
                continue
            keys.add(
                (
                    str(getattr(unit, "repo", "") or ""),
                    str(getattr(unit, "path", "") or "").replace("\\", "/").lstrip("./"),
                    str(getattr(unit, "qualified_name", "") or ""),
                )
            )
        if keys and len(keys) <= 1:
            return "self_merge"
    if action in _SINGLE_ACTIONS and len(unit_ids) < 1:
        return "empty_targets"
    return None
