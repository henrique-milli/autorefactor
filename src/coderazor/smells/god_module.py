from __future__ import annotations

from collections import defaultdict

from coderazor.models import CodeUnit, Flag, FlagSource, Severity, SuggestedAction, UnitKind, content_hash


class GodModuleSmell:
    name = "god_module"

    def __init__(self, min_children: int = 25) -> None:
        self.min_children = min_children

    def detect(self, units: list[CodeUnit]) -> list[Flag]:
        children: dict[str, list[CodeUnit]] = defaultdict(list)
        by_id = {u.id: u for u in units}
        for unit in units:
            if unit.parent_id and unit.kind in {UnitKind.FUNCTION, UnitKind.TYPE}:
                children[unit.parent_id].append(unit)

        flags: list[Flag] = []
        for parent_id, kids in children.items():
            if len(kids) < self.min_children:
                continue
            parent = by_id.get(parent_id)
            if not parent or parent.kind not in {UnitKind.MODULE, UnitKind.FILE, UnitKind.TYPE}:
                continue
            flags.append(
                Flag(
                    id=content_hash(f"god:{parent_id}:{len(kids)}"),
                    source=FlagSource.SMELL,
                    severity=Severity.HIGH if len(kids) > self.min_children * 2 else Severity.MEDIUM,
                    title=f"Oversized module/type {parent.qualified_name}",
                    evidence=f"{len(kids)} child functions/types (threshold {self.min_children})",
                    unit_ids=[parent_id, *[k.id for k in kids[:20]]],
                    suggested_action=SuggestedAction.SPLIT,
                    score=min(1.0, len(kids) / (self.min_children * 2)),
                    metadata={"child_count": len(kids)},
                )
            )
        return flags
