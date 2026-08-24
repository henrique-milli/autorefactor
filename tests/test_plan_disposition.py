"""Planner keep / review / trash disposition."""

from __future__ import annotations

from pathlib import Path

from autorefactor.agenda import build_planning_brief
from autorefactor.changesets.models import Changeset, ChangesetStatus, ChangesetTarget
from autorefactor.changesets.store import changeset_needs_hitl
from autorefactor.domain import (
    AnalysisGoal,
    CodeSpan,
    CodeUnit,
    Finding,
    PlannerDecision,
    Severity,
    SuggestedAction,
    UnitKind,
    content_hash,
)
from autorefactor.plan.planner import (
    _parse_disposition,
    decisions_to_changesets,
    plan_brief,
)


def _unit(uid: str, *, path: str, qn: str, body: str, kind: UnitKind = UnitKind.FUNCTION) -> CodeUnit:
    return CodeUnit(
        id=uid,
        repo="active",
        path=path,
        qualified_name=qn,
        kind=kind,
        language="java",
        source_snippet=body,
        content_hash=content_hash(uid),
        span=CodeSpan(start_line=1, end_line=body.count("\n") + 1),
    )


def _finding(
    fid: str,
    *,
    units: list[str],
    sensor: str = "structure",
    goal: AnalysisGoal = AnalysisGoal.STRUCTURE,
    action: SuggestedAction = SuggestedAction.SPLIT,
    score: float = 0.9,
) -> Finding:
    return Finding(
        id=fid,
        goal=goal,
        sensor=sensor,
        severity=Severity.MEDIUM,
        title=f"Finding {fid}",
        evidence="e",
        unit_ids=units,
        action_hint=action,
        score=score,
    )


class _StubProvider:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def chat_json(self, system: str, user: str, *, temperature: float = 0.2) -> dict:
        self.calls += 1
        if not self.responses:
            return {"decisions": []}
        return self.responses.pop(0)

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 3 for _ in texts]


def test_parse_disposition_defaults_to_trash() -> None:
    assert _parse_disposition({}) == "trash"
    assert _parse_disposition({"disposition": "keep"}) == "keep"
    assert _parse_disposition({"disposition": "review"}) == "review"
    assert _parse_disposition({"valid": False}) == "trash"
    assert _parse_disposition({"valid": True}) == "keep"


def test_planner_decision_safe_default_is_trash() -> None:
    d = PlannerDecision(title="x", action=SuggestedAction.MOVE)
    assert d.disposition == "trash"
    assert d.valid is False


def test_trash_emits_no_changeset() -> None:
    u = _unit("u1", path="A.java", qn="A.foo", body="void foo() { bar(); }\n" * 5)
    decision = PlannerDecision(
        title="Reject",
        action=SuggestedAction.DELETE_DUPLICATE,
        disposition="trash",
        finding_ids=["f1"],
        unit_ids=["u1"],
    )
    assert decision.valid is False
    out = decisions_to_changesets([decision], {"u1": u})
    assert out == []


def test_review_queues_with_plan_review_hitl() -> None:
    u = _unit("u1", path="A.java", qn="A.foo", body="void foo() { bar(); }\n" * 5)
    decision = PlannerDecision(
        title="Maybe",
        action=SuggestedAction.SPLIT,
        disposition="review",
        confidence=0.9,
        finding_ids=["f1"],
        unit_ids=["u1"],
        rationale="unsure",
    )
    out = decisions_to_changesets([decision], {"u1": u})
    assert len(out) == 1
    assert out[0].metrics_delta.get("plan_disposition") == "review"
    needs, reason = changeset_needs_hitl(out[0], 0.6)
    assert needs is True
    assert reason == "plan_review"


def test_keep_skips_low_confidence_hitl() -> None:
    cs = Changeset(
        id="k1",
        title="Keep low conf",
        action="split",
        brief="b",
        rationale="r",
        confidence=0.4,
        targets=[ChangesetTarget(repo="r", path="a.java", symbol="A", unit_id="a")],
        status=ChangesetStatus.PENDING,
        metrics_delta={"plan_disposition": "keep"},
    )
    needs, reason = changeset_needs_hitl(cs, 0.6)
    assert needs is False
    assert reason is None


def test_plan_brief_need_context_second_pass() -> None:
    path = "src/main/java/com/example/Service.java"
    a = _unit("a", path=path, qn="Service.one", body="void one() {\n  doA();\n}\n" * 8)
    b = _unit("b", path=path, qn="Service.two", body="void two() {\n  doB();\n}\n" * 8)
    f = _finding(
        "f-slop",
        units=["a", "b"],
        sensor="slop",
        goal=AnalysisGoal.SLOP,
        action=SuggestedAction.DELETE_DUPLICATE,
        score=0.85,
    )
    brief = build_planning_brief([f], {"a": a, "b": b}, component_id="c0")
    assert any(c.sibling_methods for c in brief.units)
    provider = _StubProvider(
        [
            {
                "decisions": [
                    {
                        "title": "maybe",
                        "action": "delete_duplicate",
                        "disposition": "review",
                        "confidence": 0.5,
                        "finding_ids": ["f-slop"],
                        "unit_ids": ["a", "b"],
                    }
                ],
                "need_context": [{"unit_id": "a"}],
            },
            {
                "decisions": [
                    {
                        "title": "trash after look",
                        "action": "delete_duplicate",
                        "disposition": "trash",
                        "confidence": 0.2,
                        "finding_ids": ["f-slop"],
                        "unit_ids": ["a", "b"],
                        "rationale": "same-file shape twin",
                    }
                ],
            },
        ]
    )
    rows = plan_brief(brief, {"a": a, "b": b}, provider)  # type: ignore[arg-type]
    assert provider.calls == 2
    assert rows[0].disposition == "trash"
    assert decisions_to_changesets(rows, {"a": a, "b": b}) == []


def test_slop_single_finding_does_not_skip_llm(tmp_path: Path) -> None:
    """Medium-risk sensors always get a disposition pass (no auto-keep skip)."""
    a = _unit(
        "a",
        path="svc/A.java",
        qn="A.compute",
        body="int compute() { return 1+2+3+4+5+6+7+8; }\n",
    )
    f = _finding(
        "f1",
        units=["a"],
        sensor="slop",
        goal=AnalysisGoal.SLOP,
        action=SuggestedAction.EXTRACT_SHARED,
        score=0.95,
    )
    brief = build_planning_brief([f], {"a": a}, component_id="c0")
    provider = _StubProvider(
        [
            {
                "decisions": [
                    {
                        "title": "nope",
                        "action": "extract_shared",
                        "disposition": "trash",
                        "finding_ids": ["f1"],
                        "unit_ids": ["a"],
                    }
                ]
            }
        ]
    )
    rows = plan_brief(brief, {"a": a}, provider)  # type: ignore[arg-type]
    assert provider.calls == 1
    assert rows[0].disposition == "trash"
