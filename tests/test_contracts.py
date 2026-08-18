from __future__ import annotations

import pytest

from autorefactor.domain import (
    AnalysisGoal,
    FindingValidationError,
    Severity,
    SuggestedAction,
    UnitAgenda,
    UnitAgendaEntry,
    make_finding,
    try_make_finding,
)


def test_make_finding_rejects_empty_unit_ids() -> None:
    with pytest.raises(FindingValidationError, match="empty unit_ids"):
        make_finding(
            id="f1",
            goal=AnalysisGoal.STRUCTURE,
            sensor="test",
            severity=Severity.LOW,
            title="t",
            evidence="e",
            unit_ids=[],
        )


def test_make_finding_rejects_unresolved() -> None:
    with pytest.raises(FindingValidationError, match="unresolved"):
        make_finding(
            id="f2",
            goal=AnalysisGoal.COVERAGE,
            sensor="coverage",
            severity=Severity.MEDIUM,
            title="t",
            evidence="e",
            unit_ids=["missing"],
            known_ids={"known"},
        )


def test_make_finding_ok() -> None:
    f = make_finding(
        id="f3",
        goal=AnalysisGoal.METHOD_SIMILARITY,
        sensor="sim.method",
        severity=Severity.HIGH,
        title="clone",
        evidence="e",
        unit_ids=["a", "b", "a"],
        action_hint=SuggestedAction.DELETE_DUPLICATE,
        known_ids={"a", "b"},
        score=0.9,
    )
    assert f.unit_ids == ["a", "b"]
    assert f.goal == AnalysisGoal.METHOD_SIMILARITY
    assert try_make_finding(
        id="bad",
        goal=AnalysisGoal.STRUCTURE,
        sensor="x",
        severity=Severity.LOW,
        title="t",
        evidence="e",
        unit_ids=[],
    ) is None


def test_unit_agenda_entry() -> None:
    agenda = UnitAgenda(
        entries=[
            UnitAgendaEntry(
                unit_id="u1",
                finding_ids=["f1", "f2"],
                goals=[AnalysisGoal.PLACEMENT, AnalysisGoal.METHOD_SIMILARITY],
            )
        ]
    )
    assert agenda.by_unit_id()["u1"].finding_ids == ["f1", "f2"]
