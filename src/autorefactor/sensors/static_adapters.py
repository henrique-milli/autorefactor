"""Adapt legacy CLI analyzer plugins into Finding sensors."""

from __future__ import annotations

from autorefactor.analyzers.base import AnalyzerContext
from autorefactor.domain import AnalysisGoal, Finding, Severity, SuggestedAction, try_make_finding
from autorefactor.sensors.base import AnalysisContext


def _adapt(
    plugin,
    *,
    sensor_id: str,
    goal: AnalysisGoal,
    ctx: AnalysisContext,
) -> list[Finding]:
    actx = AnalyzerContext(
        workspace=ctx.workspace,
        repos=list(ctx.repos),
        units=ctx.units_list(),
        config=ctx.config,
    )
    if not plugin.available(actx):
        return []
    raw = plugin.analyze(actx)
    known = ctx.known_ids()
    out: list[Finding] = []
    for flag in raw:
        # Legacy Flag/Finding-like objects
        unit_ids = list(getattr(flag, "unit_ids", []) or [])
        action = getattr(flag, "suggested_action", None) or getattr(flag, "action_hint", SuggestedAction.DOCUMENT)
        if hasattr(action, "value"):
            try:
                action = SuggestedAction(action.value)
            except ValueError:
                action = SuggestedAction.DOCUMENT
        f = try_make_finding(
            id=flag.id,
            goal=goal,
            sensor=sensor_id,
            severity=flag.severity if isinstance(flag.severity, Severity) else Severity.MEDIUM,
            title=flag.title,
            evidence=flag.evidence,
            unit_ids=unit_ids,
            action_hint=action,
            score=float(flag.score),
            metrics={},
            metadata=dict(getattr(flag, "metadata", {}) or {}),
            known_ids=known,
        )
        if f:
            out.append(f)
    return out


class SemgrepSensor:
    id = "static.semgrep"
    goal = AnalysisGoal.STATIC

    def available(self, ctx: AnalysisContext) -> bool:
        from autorefactor.sensors.static.semgrep import SemgrepAnalyzer

        return SemgrepAnalyzer().available(
            AnalyzerContext(ctx.workspace, list(ctx.repos), ctx.units_list(), ctx.config)
        )

    def collect(self, ctx: AnalysisContext) -> list[Finding]:
        from autorefactor.sensors.static.semgrep import SemgrepAnalyzer

        return _adapt(SemgrepAnalyzer(), sensor_id=self.id, goal=self.goal, ctx=ctx)


class RuffSensor:
    id = "static.ruff"
    goal = AnalysisGoal.STATIC

    def available(self, ctx: AnalysisContext) -> bool:
        from autorefactor.sensors.static.ruff_sensor import RuffAnalyzer

        return RuffAnalyzer().available(
            AnalyzerContext(ctx.workspace, list(ctx.repos), ctx.units_list(), ctx.config)
        )

    def collect(self, ctx: AnalysisContext) -> list[Finding]:
        from autorefactor.sensors.static.ruff_sensor import RuffAnalyzer

        return _adapt(RuffAnalyzer(), sensor_id=self.id, goal=self.goal, ctx=ctx)


class EslintSensor:
    id = "static.eslint"
    goal = AnalysisGoal.STATIC

    def available(self, ctx: AnalysisContext) -> bool:
        from autorefactor.sensors.static.eslint_sensor import EslintAnalyzer

        return EslintAnalyzer().available(
            AnalyzerContext(ctx.workspace, list(ctx.repos), ctx.units_list(), ctx.config)
        )

    def collect(self, ctx: AnalysisContext) -> list[Finding]:
        from autorefactor.sensors.static.eslint_sensor import EslintAnalyzer

        return _adapt(EslintAnalyzer(), sensor_id=self.id, goal=self.goal, ctx=ctx)


class JavaStaticSensor:
    id = "static.java"
    goal = AnalysisGoal.STATIC

    def available(self, ctx: AnalysisContext) -> bool:
        from autorefactor.sensors.static.java_sensor import JavaStaticAnalyzer

        return JavaStaticAnalyzer().available(
            AnalyzerContext(ctx.workspace, list(ctx.repos), ctx.units_list(), ctx.config)
        )

    def collect(self, ctx: AnalysisContext) -> list[Finding]:
        from autorefactor.sensors.static.java_sensor import JavaStaticAnalyzer

        return _adapt(JavaStaticAnalyzer(), sensor_id=self.id, goal=self.goal, ctx=ctx)
