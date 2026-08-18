"""Reject no-op merge/delete_duplicate when targets collapse to one symbol."""

from __future__ import annotations

from autorefactor.domain import CodeUnit, PlannerDecision, SuggestedAction, UnitKind, content_hash
from autorefactor.plan.planner import decisions_to_changesets
from autorefactor.sensors.validate import decision_structural_reason


def _unit(uid: str, *, qname: str, path: str = "src/Foo.java") -> CodeUnit:
    return CodeUnit(
        id=uid,
        repo="janus-account",
        path=path,
        language="java",
        kind=UnitKind.FUNCTION,
        qualified_name=qname,
        source_snippet="void m() {}",
        content_hash=content_hash(uid),
    )


def test_self_merge_rejected_for_identical_symbol_pair() -> None:
    u1 = _unit("u1", qname="com.example.RegulatorApiTemplate.fillCommunicationLayers")
    u2 = _unit("u2", qname="com.example.RegulatorApiTemplate.fillCommunicationLayers")
    units_by_id = {u1.id: u1, u2.id: u2}
    payload = {
        "valid": True,
        "action": "merge",
        "unit_ids": [u1.id, u2.id],
    }
    assert decision_structural_reason(payload, set(units_by_id), units_by_id) == "self_merge"


def test_merge_distinct_symbols_still_valid() -> None:
    u1 = _unit("u1", qname="com.example.A.m")
    u2 = _unit("u2", qname="com.example.B.m", path="src/Bar.java")
    units_by_id = {u1.id: u1, u2.id: u2}
    payload = {
        "valid": True,
        "action": "merge",
        "unit_ids": [u1.id, u2.id],
    }
    assert decision_structural_reason(payload, set(units_by_id), units_by_id) is None


def test_decisions_to_changesets_drops_self_merge() -> None:
    u1 = _unit("u1", qname="com.example.RegulatorApiTemplate.fillRegulatorLayer")
    u2 = _unit("u2", qname="com.example.RegulatorApiTemplate.fillRegulatorLayer")
    units_by_id = {u1.id: u1, u2.id: u2}
    decision = PlannerDecision(
        title="Merge duplicates",
        action=SuggestedAction.MERGE,
        rationale="similar",
        confidence=0.99,
        finding_ids=["f1"],
        unit_ids=[u1.id, u2.id],
        valid=True,
    )
    out = decisions_to_changesets([decision], units_by_id)
    assert out == []


def test_distinct_regulator_message_returns_not_delete_duplicate() -> None:
    from autorefactor.domain import AnalysisGoal
    from autorefactor.sensors.similarity import emit_similarity_findings

    def _handler(uid: str, cls: str, const: str, emb: list[float]) -> CodeUnit:
        src = (
            f"public RegulatorMessage getRegulatorMessage() {{\n"
            f"    return RegulatorMessage.{const};\n"
            f"}}\n"
        )
        return CodeUnit(
            id=uid,
            repo="janus-account",
            path=f"src/main/java/com/example/{cls}.java",
            language="java",
            kind=UnitKind.FUNCTION,
            qualified_name=f"com.example.{cls}.getRegulatorMessage",
            source_snippet=src + ("    // pad\n" * 20),
            content_hash=content_hash(uid),
            embedding=emb,
        )

    a = _handler("a", "HandlerA", "CLIENT_PS_001", [1.0, 0.0, 0.0])
    b = _handler("b", "HandlerB", "CLIENT_PS_002", [0.99, 0.01, 0.0])
    findings = emit_similarity_findings(
        [a, b],
        kinds={UnitKind.FUNCTION},
        goal=AnalysisGoal.METHOD_SIMILARITY,
        sensor="sim.method",
        threshold=0.5,
        max_pairs=10,
    )
    assert findings == []


def test_fingerprint_skips_enum_matrix_handlers() -> None:
    from autorefactor.config import AutorefactorConfig
    from autorefactor.sensors.base import AnalysisContext
    from autorefactor.sensors.fingerprint import FingerprintCloneSensor

    def _fn(uid: str, cls: str, const: str) -> CodeUnit:
        body = (
            "public RegulatorMessage getRegulatorMessage(Request r) {\n"
            "    if (r == null) { return RegulatorMessage.UNKNOWN; }\n"
            f"    return RegulatorMessage.{const};\n"
            "}\n"
        )
        return CodeUnit(
            id=uid,
            repo="janus-account",
            path=f"src/main/java/{cls}.java",
            language="java",
            kind=UnitKind.FUNCTION,
            qualified_name=f"{cls}.mapOther",
            source_snippet=body,
            content_hash=content_hash(uid),
        )

    a = _fn("fa", "HandlerA", "CLIENT_PS_001")
    b = _fn("fb", "HandlerB", "CLIENT_PS_002")
    ctx = AnalysisContext(
        workspace=__import__("pathlib").Path("."),
        repos=(),
        units=(a, b),
        config=AutorefactorConfig(skip_llm=True),
        focus="balanced",
    )
    findings = FingerprintCloneSensor().collect(ctx)
    assert not any(f.action_hint.value == "delete_duplicate" for f in findings)
