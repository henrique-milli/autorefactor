from __future__ import annotations

from coderazor.models import CodeUnit, Flag, FlagSource, Severity, SuggestedAction, UnitKind, content_hash


class LargeFileSmell:
    name = "large_file"

    def __init__(self, max_lines: int = 500) -> None:
        self.max_lines = max_lines

    def detect(self, units: list[CodeUnit]) -> list[Flag]:
        flags: list[Flag] = []
        for unit in units:
            if unit.kind != UnitKind.FILE:
                continue
            lines = unit.span.end_line or (unit.source_snippet.count("\n") + 1)
            if lines < self.max_lines:
                continue
            flags.append(
                Flag(
                    id=content_hash(f"largefile:{unit.id}:{lines}"),
                    source=FlagSource.SMELL,
                    severity=Severity.MEDIUM if lines < self.max_lines * 2 else Severity.HIGH,
                    title=f"Large file {unit.path}",
                    evidence=f"{lines} lines (threshold {self.max_lines})",
                    unit_ids=[unit.id],
                    suggested_action=SuggestedAction.SPLIT,
                    score=min(1.0, lines / (self.max_lines * 2)),
                    metadata={"lines": lines},
                )
            )
        return flags
