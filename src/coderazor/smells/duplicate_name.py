from __future__ import annotations

from collections import defaultdict

from coderazor.index.hierarchy import simple_name
from coderazor.models import (
    CodeUnit,
    Flag,
    FlagSource,
    Severity,
    SuggestedAction,
    UnitKind,
    content_hash,
)


class DuplicateNameSmell:
    name = "duplicate_name"

    def detect(self, units: list[CodeUnit]) -> list[Flag]:
        by_name: dict[str, list[CodeUnit]] = defaultdict(list)
        for unit in units:
            if unit.kind not in {UnitKind.FUNCTION, UnitKind.TYPE, UnitKind.PROTO_MESSAGE, UnitKind.PROTO_RPC}:
                continue
            by_name[simple_name(unit.qualified_name)].append(unit)

        flags: list[Flag] = []
        for name, group in by_name.items():
            repos = {u.repo for u in group}
            if len(repos) < 2:
                continue
            flags.append(
                Flag(
                    id=content_hash(f"dupname:{name}:{','.join(sorted(u.id for u in group))}"),
                    source=FlagSource.SMELL,
                    severity=Severity.MEDIUM,
                    title=f"Duplicate name '{name}' across repos",
                    evidence=f"Found in repos: {', '.join(sorted(repos))}",
                    unit_ids=[u.id for u in group],
                    suggested_action=SuggestedAction.EXTRACT_SHARED,
                    score=0.7,
                    metadata={"name": name, "repos": sorted(repos)},
                )
            )
        return flags
