from __future__ import annotations

from collections import defaultdict

from autorefactor.domain import (
    AnalysisGoal,
    CodeUnit,
    Finding,
    PlanningBrief,
    SuggestedAction,
    UnitAgenda,
    UnitAgendaEntry,
    UnitCard,
)


def build_unit_agenda(findings: list[Finding]) -> UnitAgenda:
    by_unit: dict[str, list[Finding]] = defaultdict(list)
    for finding in findings:
        for uid in finding.unit_ids:
            by_unit[uid].append(finding)
    entries: list[UnitAgendaEntry] = []
    for unit_id, unit_findings in sorted(by_unit.items()):
        goals: list[AnalysisGoal] = []
        seen: set[AnalysisGoal] = set()
        for f in unit_findings:
            if f.goal not in seen:
                seen.add(f.goal)
                goals.append(f.goal)
        entries.append(
            UnitAgendaEntry(
                unit_id=unit_id,
                finding_ids=[f.id for f in unit_findings],
                goals=goals,
            )
        )
    return UnitAgenda(entries=entries)


def conflict_components(
    findings: list[Finding],
    units_by_id: dict[str, CodeUnit] | None = None,
) -> list[list[Finding]]:
    if not findings:
        return []
    unit_to_flags: dict[str, list[int]] = {}
    for idx, finding in enumerate(findings):
        for uid in finding.unit_ids:
            unit_to_flags.setdefault(uid, []).append(idx)
    parent = list(range(len(findings)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for indices in unit_to_flags.values():
        for i in range(1, len(indices)):
            union(indices[0], indices[i])

    # Link sibling long-function / structure findings that share a parent type
    if units_by_id:
        by_parent: dict[str, list[int]] = defaultdict(list)
        for idx, finding in enumerate(findings):
            if finding.sensor != "structure" or finding.action_hint != SuggestedAction.SPLIT:
                continue
            if len(finding.unit_ids) != 1:
                continue
            unit = units_by_id.get(finding.unit_ids[0])
            if not unit or not unit.parent_id:
                continue
            by_parent[unit.parent_id].append(idx)
        for indices in by_parent.values():
            if len(indices) < 2:
                continue
            for i in range(1, len(indices)):
                union(indices[0], indices[i])

    buckets: dict[int, list[Finding]] = {}
    for idx, finding in enumerate(findings):
        buckets.setdefault(find(idx), []).append(finding)
    return list(buckets.values())


def split_oversized_components(
    components: list[list[Finding]],
    *,
    max_size: int = 8,
) -> list[list[Finding]]:
    """Cap planning-brief size so the LLM can cover findings.

    Giant union-find components (shared units / sibling splits) otherwise collapse
    into one brief; the planner then silently drops most finding_ids.
    Prefer splitting by goal, then by descending score into chunks of ``max_size``.
    """
    if max_size < 1:
        max_size = 1
    out: list[list[Finding]] = []
    for comp in components:
        if len(comp) <= max_size:
            out.append(comp)
            continue
        by_goal: dict[AnalysisGoal, list[Finding]] = defaultdict(list)
        for finding in comp:
            by_goal[finding.goal].append(finding)
        for items in by_goal.values():
            items = sorted(items, key=lambda f: (-f.score, f.id))
            for i in range(0, len(items), max_size):
                chunk = items[i : i + max_size]
                if chunk:
                    out.append(chunk)
    return out


def build_planning_brief(
    component: list[Finding],
    units_by_id: dict[str, CodeUnit],
    *,
    component_id: str,
) -> PlanningBrief:
    unit_ids = sorted({uid for f in component for uid in f.unit_ids})
    cards: list[UnitCard] = []
    for uid in unit_ids:
        u = units_by_id.get(uid)
        if not u:
            continue
        cards.append(
            UnitCard(
                unit_id=u.id,
                kind=u.kind.value,
                repo=u.repo,
                path=u.path,
                qualified_name=u.qualified_name,
                description=(u.description or u.source_snippet[:80]).replace("\n", " ")[:120],
            )
        )
    by_goal: dict[str, list[str]] = defaultdict(list)
    hints: list[str] = []
    actions: list[SuggestedAction] = []
    for f in component:
        by_goal[f.goal.value].append(f.id)
        for h in f.hints:
            if h not in hints:
                hints.append(h)
        if f.action_hint not in actions:
            actions.append(f.action_hint)
    return PlanningBrief(
        component_id=component_id,
        units=cards,
        findings=component,
        findings_by_goal=dict(by_goal),
        hints=hints,
        candidate_actions=actions,
    )
