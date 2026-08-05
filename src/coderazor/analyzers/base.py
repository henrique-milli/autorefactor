from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from coderazor.config import CoderazorConfig
from coderazor.models import AnalyzerRunNote, CodeUnit, Flag


@dataclass
class AnalyzerContext:
    workspace: Path
    repos: list[Path]
    units: list[CodeUnit]
    config: CoderazorConfig
    notes: list[AnalyzerRunNote] = field(default_factory=list)

    def note(self, name: str, status: str, detail: str = "", flag_count: int = 0) -> None:
        self.notes.append(
            AnalyzerRunNote(name=name, status=status, detail=detail, flag_count=flag_count)
        )


class AnalyzerPlugin(Protocol):
    name: str

    def available(self, ctx: AnalyzerContext) -> bool:
        ...

    def analyze(self, ctx: AnalyzerContext) -> list[Flag]:
        ...
