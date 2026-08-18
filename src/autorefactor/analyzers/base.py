from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from autorefactor.config import AutorefactorConfig
from autorefactor.domain import CodeUnit, Finding, SensorReport


@dataclass
class AnalyzerContext:
    workspace: Path
    repos: list[Path]
    units: list[CodeUnit]
    config: AutorefactorConfig
    notes: list[SensorReport] = field(default_factory=list)

    def note(self, name: str, status: str, detail: str = "", flag_count: int = 0) -> None:
        self.notes.append(
            SensorReport(sensor=name, status=status, detail=detail, finding_count=flag_count)
        )


class AnalyzerPlugin(Protocol):
    name: str

    def available(self, ctx: AnalyzerContext) -> bool:
        ...

    def analyze(self, ctx: AnalyzerContext) -> list[Finding]:
        ...
