"""Backward-compatible re-exports from autorefactor.domain (0.2)."""

from typing import Any

from autorefactor.domain import (
    SCHEMA_VERSION,
    FOCUS_GOALS,
    AnalysisArtifacts,
    AnalysisGoal,
    CodeSpan,
    CodeUnit,
    ConfirmedFinding,
    CoverageFileStat,
    CoverageSnapshot,
    DescriptionResult,
    DocumentGap,
    DocumentReport,
    Finding,
    FindingValidationError,
    PlanStep,
    PlannerDecision,
    PlanningBrief,
    RefactorPlan,
    RunManifest,
    SensorReport,
    Severity,
    SuggestedAction,
    TestGap,
    TestHarnessResult,
    TestPlan,
    UnitAgenda,
    UnitAgendaEntry,
    UnitCard,
    UnitKind,
    content_hash,
    make_finding,
    try_make_finding,
    unit_id,
    utc_now_iso,
)

Flag = Finding
FlagValidationError = FindingValidationError
AnalyzerRunNote = SensorReport


def make_flag(**kwargs: Any) -> Finding:
    if "suggested_action" in kwargs and "action_hint" not in kwargs:
        kwargs["action_hint"] = kwargs.pop("suggested_action")
    kwargs.pop("source", None)
    return make_finding(**kwargs)


def try_make_flag(**kwargs: Any) -> Finding | None:
    try:
        return make_flag(**kwargs)
    except FindingValidationError:
        return None


def goal_to_source(goal: AnalysisGoal):  # noqa: ARG001
    raise RuntimeError("FlagSource removed in 0.2 — use Finding.goal")


__all__ = [
    "SCHEMA_VERSION",
    "FOCUS_GOALS",
    "AnalysisArtifacts",
    "AnalysisGoal",
    "AnalyzerRunNote",
    "CodeSpan",
    "CodeUnit",
    "ConfirmedFinding",
    "CoverageFileStat",
    "CoverageSnapshot",
    "DescriptionResult",
    "DocumentGap",
    "DocumentReport",
    "Finding",
    "FindingValidationError",
    "Flag",
    "FlagValidationError",
    "PlanStep",
    "PlannerDecision",
    "PlanningBrief",
    "RefactorPlan",
    "RunManifest",
    "SensorReport",
    "Severity",
    "SuggestedAction",
    "TestGap",
    "TestHarnessResult",
    "TestPlan",
    "UnitAgenda",
    "UnitAgendaEntry",
    "UnitCard",
    "UnitKind",
    "content_hash",
    "goal_to_source",
    "make_finding",
    "make_flag",
    "try_make_finding",
    "try_make_flag",
    "unit_id",
    "utc_now_iso",
]
