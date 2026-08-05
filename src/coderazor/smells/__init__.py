"""Deprecated: smells live under ``coderazor.analyzers.builtin``.

Kept as thin wrappers so older imports keep working during migration.
"""

from __future__ import annotations

from coderazor.analyzers.base import AnalyzerContext
from coderazor.analyzers.builtin import BuiltinAnalyzerBundle
from coderazor.config import CoderazorConfig
from coderazor.models import CodeUnit, Flag


def run_smell_detectors(units: list[CodeUnit], config: CoderazorConfig) -> list[Flag]:
    ctx = AnalyzerContext(
        workspace=config.workspace,
        repos=config.discover_repos(),
        units=units,
        config=config,
    )
    return BuiltinAnalyzerBundle().analyze(ctx)
