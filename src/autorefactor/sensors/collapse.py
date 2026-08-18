"""Collapse overlapping sensor findings and demote vanity splits."""

from __future__ import annotations

from autorefactor.domain import Finding, SuggestedAction

_CLONE_SENSORS = frozenset({"sim.fingerprint", "sim.method", "sim.class", "slop", "cluster"})
_SPLIT_SENSORS = frozenset({"structure", "complexity"})
_CLONE_ACTIONS = frozenset(
    {
        SuggestedAction.DELETE_DUPLICATE,
        SuggestedAction.MERGE,
        SuggestedAction.EXTRACT_SHARED,
    }
)


def _unit_key(finding: Finding) -> frozenset[str]:
    return frozenset(finding.unit_ids)


def _is_clone_track(finding: Finding) -> bool:
    if finding.sensor in _CLONE_SENSORS:
        return True
    return finding.action_hint in _CLONE_ACTIONS and finding.sensor not in _SPLIT_SENSORS


def _is_vanity_split(finding: Finding) -> bool:
    return finding.action_hint == SuggestedAction.SPLIT and (
        finding.sensor in _SPLIT_SENSORS or finding.goal.value == "structure"
    )


def _rank(finding: Finding) -> tuple:
    """Higher is better when choosing a winner in a unit-set group."""
    return (
        1 if _is_clone_track(finding) else 0,
        0 if _is_vanity_split(finding) else 1,
        float(finding.score or 0.0),
        # Prefer richer evidence titles last as tie-break stability via id
        finding.id,
    )


def collapse_findings_by_unit_set(findings: list[Finding]) -> list[Finding]:
    """Collapse same/overlapping unit-sets; demote structure/complexity splits under clones.

    Rules:
    - Exact frozenset(unit_ids): keep highest-ranked finding; stash collapsed_from.
    - Subset unit-sets: drop vanity splits that are ⊆ a kept clone finding's units.
    - Global demotion: drop structure/complexity SPLIT that share any unit with a
      kept extract/merge/delete clone finding.
    """
    if not findings:
        return []

    # Phase 1: collapse exact unit-set duplicates
    by_key: dict[frozenset[str], list[Finding]] = {}
    for f in findings:
        by_key.setdefault(_unit_key(f), []).append(f)

    winners: list[Finding] = []
    for key, group in by_key.items():
        if not key:
            winners.extend(group)
            continue
        ordered = sorted(group, key=_rank, reverse=True)
        best = ordered[0]
        if len(ordered) > 1:
            collapsed_ids = [g.id for g in ordered[1:]]
            meta = dict(best.metadata or {})
            meta["collapsed_from"] = collapsed_ids
            best = best.model_copy(update={"metadata": meta})
        winners.append(best)

    # Phase 2: demote vanity splits that overlap clone-track findings
    clone_units: set[str] = set()
    for f in winners:
        if _is_clone_track(f) and f.action_hint in _CLONE_ACTIONS:
            clone_units.update(f.unit_ids)

    kept: list[Finding] = []
    for f in winners:
        if _is_vanity_split(f) and clone_units.intersection(f.unit_ids):
            # Drop — superseded by clone work on the same units
            continue
        # Subset vanity split under a larger clone set already covered by intersection
        kept.append(f)

    # Phase 3: drop vanity splits whose unit set is a subset of another kept finding's
    # clone units (exact key already collapsed; this catches structure-only singles
    # under multi-unit clone pairs).
    clone_sets = [_unit_key(f) for f in kept if _is_clone_track(f) and f.action_hint in _CLONE_ACTIONS]
    final: list[Finding] = []
    for f in kept:
        key = _unit_key(f)
        if _is_vanity_split(f) and any(key and key <= cs for cs in clone_sets):
            continue
        final.append(f)
    return final
