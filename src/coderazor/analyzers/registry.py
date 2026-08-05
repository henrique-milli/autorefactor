from __future__ import annotations

from coderazor.analyzers.base import AnalyzerContext, AnalyzerPlugin
from coderazor.analyzers.builtin import BuiltinAnalyzerBundle
from coderazor.analyzers.coverage_import import CoverageImportAnalyzer
from coderazor.analyzers.eslint_analyzer import EslintAnalyzer
from coderazor.analyzers.java_static import JavaStaticAnalyzer
from coderazor.analyzers.ruff_analyzer import RuffAnalyzer
from coderazor.analyzers.semgrep import SemgrepAnalyzer
from coderazor.models import Flag


def build_analyzer_registry() -> dict[str, AnalyzerPlugin]:
    plugins: list[AnalyzerPlugin] = [
        BuiltinAnalyzerBundle(),
        SemgrepAnalyzer(),
        RuffAnalyzer(),
        EslintAnalyzer(),
        JavaStaticAnalyzer(),
        CoverageImportAnalyzer(),
    ]
    return {p.name: p for p in plugins}


def run_analyzers(ctx: AnalyzerContext) -> list[Flag]:
    registry = build_analyzer_registry()
    enabled = ctx.config.enabled_analyzers()
    flags: list[Flag] = []
    for name in enabled:
        plugin = registry.get(name)
        if plugin is None:
            ctx.note(name, "skipped", "unknown analyzer")
            continue
        if not plugin.available(ctx):
            ctx.note(name, "skipped", "tool or inputs not available")
            continue
        try:
            produced = plugin.analyze(ctx)
        except Exception as exc:  # noqa: BLE001 — isolate plugin failures
            ctx.note(name, "error", str(exc)[:300])
            continue
        flags.extend(produced)
        ctx.note(name, "ran", flag_count=len(produced))
    return flags
