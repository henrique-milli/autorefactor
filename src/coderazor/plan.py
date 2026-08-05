from __future__ import annotations

from coderazor.models import (
    CodeUnit,
    ConfirmedFinding,
    PlanStep,
    RefactorPlan,
    SuggestedAction,
)


def _blast_radius(action: SuggestedAction, finding: ConfirmedFinding, units_by_id: dict[str, CodeUnit]) -> str:
    repos = {
        units_by_id[uid].repo
        for uid in finding.flag.unit_ids
        if uid in units_by_id
    }
    if action in {
        SuggestedAction.EXTRACT_SHARED,
        SuggestedAction.MERGE,
        SuggestedAction.DELETE_DUPLICATE,
    }:
        return "high" if len(repos) > 1 else "medium"
    if action == SuggestedAction.SPLIT:
        return "medium"
    if action == SuggestedAction.DOCUMENT:
        return "low"
    return "medium"


def _finding_score(finding: ConfirmedFinding) -> float:
    return finding.flag.score


def build_plan(
    findings: list[ConfirmedFinding],
    units: list[CodeUnit],
    *,
    tests_required: bool = True,
    skip_document_actions: bool = False,
    path_cap: int = 80,
) -> RefactorPlan:
    """Build a ranked refactor plan from confirmed findings.

    Testing is owned by ``coderazor test`` — this plan does **not** inject an S00
    ensure_tests step. When ``tests_required`` is true the summary reminds the
    operator to run the test stage before refactor.
    """
    by_id = {u.id: u for u in units}
    confirmed = [f for f in findings if f.valid]
    rejected = [f for f in findings if not f.valid]

    actionable: list[tuple[SuggestedAction, ConfirmedFinding]] = []
    for finding in confirmed:
        action = finding.refined_action or finding.flag.suggested_action
        if action == SuggestedAction.ENSURE_TESTS:
            continue
        if skip_document_actions and action == SuggestedAction.DOCUMENT:
            continue
        actionable.append((action, finding))

    # Rank: severity/score first, then group by action for step cohesion
    actionable.sort(
        key=lambda pair: (
            -_finding_score(pair[1]),
            pair[0].value,
            pair[1].flag.title,
        )
    )

    priority = [
        SuggestedAction.DELETE_DUPLICATE,
        SuggestedAction.EXTRACT_SHARED,
        SuggestedAction.MERGE,
        SuggestedAction.MOVE,
        SuggestedAction.SPLIT,
        SuggestedAction.DOCUMENT,
    ]
    by_action: dict[SuggestedAction, list[ConfirmedFinding]] = {a: [] for a in priority}
    for action, finding in actionable:
        by_action.setdefault(action, []).append(finding)

    steps: list[PlanStep] = []
    step_idx = 1
    for action in priority:
        bucket = by_action.get(action) or []
        if not bucket:
            continue
        unit_ids: list[str] = []
        paths: list[str] = []
        seen_paths: set[str] = set()
        for finding in bucket:
            unit_ids.extend(finding.flag.unit_ids)
            for uid in finding.flag.unit_ids:
                unit = by_id.get(uid)
                if not unit:
                    continue
                path = f"{unit.repo}/{unit.path}"
                if path not in seen_paths:
                    seen_paths.add(path)
                    paths.append(path)
        remainder = max(0, len(paths) - path_cap)
        shown = paths[:path_cap]
        if remainder:
            shown.append(f"… and {remainder} more path(s)")

        blasts = [_blast_radius(action, f, by_id) for f in bucket]
        blast = "high" if "high" in blasts else ("medium" if "medium" in blasts else "low")
        rationale = (
            f"{len(bucket)} confirmed finding(s). "
            f"Top: {bucket[0].flag.title} — {bucket[0].reasoning[:240]}"
        )
        steps.append(
            PlanStep(
                step_id=f"S{step_idx:02d}",
                title=f"{action.value}: {len(bucket)} confirmed finding(s)",
                action=action,
                finding_ids=[f.flag.id for f in bucket],
                unit_ids=sorted(set(unit_ids)),
                target_paths=shown,
                blast_radius=blast,
                acceptance="Tests still pass; no behavior regressions.",
                rationale=rationale,
            )
        )
        step_idx += 1

    gate_note = (
        " Run `coderazor test` and obtain a green harness before `coderazor refactor`."
        if tests_required
        else ""
    )
    summary = (
        f"coderazor confirmed {len(confirmed)} of {len(findings)} flags "
        f"({len(rejected)} rejected). Proposed {len(steps)} refactor step(s)."
        f"{gate_note}"
    )
    return RefactorPlan(
        summary=summary,
        steps=steps,
        confirmed_count=len(confirmed),
        rejected_count=len(rejected),
        tests_required=tests_required,
    )
