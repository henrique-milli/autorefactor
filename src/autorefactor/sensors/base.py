from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from autorefactor.config import AutorefactorConfig
from autorefactor.domain import AnalysisGoal, CodeUnit, CoverageSnapshot, Finding, SensorReport


@dataclass(frozen=True)
class AnalysisContext:
    """Immutable snapshot after enrich — sensors must not mutate shared state."""

    workspace: Path
    repos: tuple[Path, ...]
    units: tuple[CodeUnit, ...]
    config: AutorefactorConfig
    focus: str
    coverage: CoverageSnapshot | None = None

    def known_ids(self) -> set[str]:
        return {u.id for u in self.units}

    def units_list(self) -> list[CodeUnit]:
        return list(self.units)


class Sensor(Protocol):
    id: str
    goal: AnalysisGoal

    def available(self, ctx: AnalysisContext) -> bool:
        ...

    def collect(self, ctx: AnalysisContext) -> list[Finding]:
        ...


@dataclass
class SensorRunResult:
    findings: list[Finding]
    report: SensorReport
