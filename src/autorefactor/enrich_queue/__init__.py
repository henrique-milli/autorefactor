from __future__ import annotations

import hashlib
from typing import Any

from autorefactor.domain import CodeUnit, Finding, UnitKind
from autorefactor.enrich_queue.models import EnrichTask, EnrichTaskKind, EnrichTaskStatus
from autorefactor.enrich_queue.store import EnrichTaskStore, enrich_queue_path


def _task_id(kind: str, key: str) -> str:
    digest = hashlib.sha1(f"{kind}:{key}".encode()).hexdigest()[:12]
    return f"enrich-{kind}-{digest}"


def build_enrich_tasks(
    *,
    findings: list[Finding],
    units: list[CodeUnit],
    max_describe: int = 40,
    include_plan: bool = True,
    max_plan_component_size: int = 8,
) -> list[EnrichTask]:
    """Build describe (+ optional plan) enrich tasks for agent-mode analyze."""
    units_by_id = {u.id: u for u in units}
    finding_unit_ids: list[str] = []
    seen: set[str] = set()
    for f in sorted(findings, key=lambda x: -x.score):
        for uid in f.unit_ids:
            if uid not in seen and uid in units_by_id:
                seen.add(uid)
                finding_unit_ids.append(uid)

    tasks: list[EnrichTask] = []
    for rank, uid in enumerate(finding_unit_ids[: max(0, max_describe)]):
        unit = units_by_id[uid]
        if unit.kind in {UnitKind.REPO, UnitKind.MODULE}:
            continue
        tasks.append(
            EnrichTask(
                id=_task_id("describe", uid),
                kind=EnrichTaskKind.DESCRIBE,
                priority=1000 - rank,
                payload={
                    "unit_id": unit.id,
                    "repo": unit.repo,
                    "path": unit.path,
                    "qualified_name": unit.qualified_name,
                    "kind": unit.kind.value if hasattr(unit.kind, "value") else str(unit.kind),
                    "language": unit.language,
                    "source_snippet": (unit.source_snippet or "")[:4000],
                    "instruction": (
                        "Write a 40-80 word behavioral summary. "
                        'Return JSON: {"doc": "...", "tags": ["..."]}.'
                    ),
                },
            )
        )

    if include_plan and findings:
        from autorefactor.agenda import conflict_components, split_oversized_components

        components = split_oversized_components(
            conflict_components(findings, units_by_id),
            max_size=max(1, max_plan_component_size),
        )
        for i, comp in enumerate(components):
            key = ",".join(sorted(f.id for f in comp))
            tasks.append(
                EnrichTask(
                    id=_task_id("plan", key),
                    kind=EnrichTaskKind.PLAN,
                    priority=500 - i,
                    payload={
                        "component_id": f"c{i}",
                        "findings": [
                            {
                                "id": f.id,
                                "title": f.title,
                                "action_hint": (
                                    f.action_hint.value
                                    if hasattr(f.action_hint, "value")
                                    else str(f.action_hint)
                                ),
                                "score": f.score,
                                "unit_ids": list(f.unit_ids),
                                "evidence": (f.evidence or "")[:800],
                            }
                            for f in comp
                        ],
                        "instruction": (
                            "Plan refactor actions for this finding cluster. "
                            'Return JSON: {"decisions": [{"title", "action", "rationale", '
                            '"confidence", "finding_ids", "unit_ids", "valid"}]}.'
                        ),
                    },
                )
            )
    return tasks


def save_enrich_tasks(out_dir, tasks: list[EnrichTask]) -> Any:  # noqa: ANN001
    path = enrich_queue_path(out_dir)
    EnrichTaskStore(path).replace_all(tasks)
    return path


def pending_enrich_count(out_dir) -> int:  # noqa: ANN001
    store = EnrichTaskStore(enrich_queue_path(out_dir))
    return sum(
        1
        for t in store.load()
        if t.status in {EnrichTaskStatus.PENDING, EnrichTaskStatus.IN_PROGRESS}
    )
