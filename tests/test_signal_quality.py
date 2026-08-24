from __future__ import annotations

from pathlib import Path

import numpy as np

from autorefactor.config import AutorefactorConfig
from autorefactor.domain import (
    CodeSpan,
    CodeUnit,
    CoverageFileStat,
    CoverageSnapshot,
    UnitKind,
    content_hash,
    unit_id,
)
from autorefactor.plan.planner import _estimate_loc_delta
from autorefactor.sensors.base import AnalysisContext
from autorefactor.sensors.coverage import CoverageSensor
from autorefactor.sensors.placement import PlacementSensor
from autorefactor.sensors.similarity import MethodSimilaritySensor, _is_low_signal, emit_similarity_findings
from autorefactor.sensors.structure import StructureSensor
from autorefactor.domain import AnalysisGoal, SuggestedAction


def _emb(*vals: float) -> list[float]:
    v = np.array(vals, dtype=np.float64)
    n = float(np.linalg.norm(v))
    return (v / n).tolist() if n else vals  # type: ignore[return-value]


def _unit(
    key: str,
    *,
    path: str,
    qn: str,
    kind: UnitKind = UnitKind.FUNCTION,
    snippet: str | None = None,
    start: int = 1,
    end: int = 10,
    embedding: list[float] | None = None,
    parent_id: str | None = None,
    repo: str = "demo",
) -> CodeUnit:
    body = snippet or ("void body() {\n    doWork();\n    return;\n}\n" * 5)
    return CodeUnit(
        id=unit_id(key),
        repo=repo,
        path=path,
        qualified_name=qn,
        kind=kind,
        language="java",
        source_snippet=body,
        content_hash=content_hash(key),
        span=CodeSpan(start_line=start, end_line=end),
        embedding=embedding,
        parent_id=parent_id,
    )


def _ctx(units: list[CodeUnit], **cfg_kw) -> AnalysisContext:
    cfg = AutorefactorConfig(skip_llm=True, workspace=Path("/tmp"), **cfg_kw)
    return AnalysisContext(
        workspace=Path("/tmp"),
        repos=(Path("/tmp/demo"),),
        units=tuple(units),
        config=cfg,
        focus=cfg_kw.get("focus", "balanced"),
    )


def test_low_signal_includes_short_getters() -> None:
    getter = _unit(
        "g",
        path="src/main/java/A.java",
        qn="A.getName",
        snippet="public String getName() { return name; }\n",
        embedding=_emb(1, 0, 0),
    )
    assert _is_low_signal(getter)


def test_placement_below_floor_emits_nothing() -> None:
    owner = _unit(
        "owner",
        path="src/main/java/Owner.java",
        qn="Owner",
        kind=UnitKind.TYPE,
        embedding=_emb(1, 0, 0),
    )
    other = _unit(
        "other",
        path="src/main/java/Other.java",
        qn="Other",
        kind=UnitKind.TYPE,
        embedding=_emb(0, 1, 0),
    )
    # Best cosine ~0.75 < 0.82 → old new-type band; must emit nothing now
    fn = _unit(
        "fn",
        path="src/main/java/Owner.java",
        qn="Owner.doThing",
        embedding=_emb(0.75, 0.661, 0),
        parent_id=owner.id,
        snippet="void doThing() {\n" + "    step();\n" * 20 + "}\n",
    )
    findings = PlacementSensor().collect(_ctx([fn, owner, other]))
    assert findings == []


def test_placement_skips_test_path_and_ctor() -> None:
    t1 = _unit(
        "t1",
        path="src/main/java/A.java",
        qn="A",
        kind=UnitKind.TYPE,
        embedding=_emb(1, 0, 0),
    )
    t2 = _unit(
        "t2",
        path="src/main/java/B.java",
        qn="B",
        kind=UnitKind.TYPE,
        embedding=_emb(0.99, 0.14, 0),
    )
    test_fn = _unit(
        "testfn",
        path="src/test/java/ATest.java",
        qn="ATest.doThing",
        embedding=_emb(0.99, 0.14, 0),
        parent_id=t1.id,
        snippet="void doThing() {\n" + "    step();\n" * 20 + "}\n",
    )
    ctor = _unit(
        "ctor",
        path="src/main/java/A.java",
        qn="A.A",
        embedding=_emb(0.99, 0.14, 0),
        parent_id=t1.id,
        snippet="public A(String x) {\n" + "    this.x = x;\n" * 10 + "}\n",
    )
    assert PlacementSensor().collect(_ctx([test_fn, ctor, t1, t2])) == []


def test_placement_emits_move_when_better_type() -> None:
    owner = _unit(
        "owner",
        path="src/main/java/Owner.java",
        qn="Owner",
        kind=UnitKind.TYPE,
        embedding=_emb(1, 0, 0),
    )
    better = _unit(
        "better",
        path="src/main/java/Better.java",
        qn="Better",
        kind=UnitKind.TYPE,
        embedding=_emb(0, 1, 0),
    )
    fn = _unit(
        "fn",
        path="src/main/java/Owner.java",
        qn="Owner.work",
        embedding=_emb(0, 1, 0),
        parent_id=owner.id,
        snippet="void work() {\n" + "    compute();\n" * 20 + "}\n",
    )
    findings = PlacementSensor().collect(_ctx([fn, owner, better]))
    assert len(findings) == 1
    assert findings[0].action_hint == SuggestedAction.MOVE
    assert "may need new type" not in findings[0].title
    assert better.id in findings[0].unit_ids


def test_placement_skips_mapper_into_aggregated_data() -> None:
    """Mapper method embedding-matched to its param DTO must not become a move."""
    mapper = _unit(
        "mapper_type",
        path="src/main/java/com/example/mapper/BonusResponseMapper.java",
        qn="com.example.mapper.BonusResponseMapper",
        kind=UnitKind.TYPE,
        embedding=_emb(1, 0, 0),
    )
    data = _unit(
        "agg_data",
        path="src/main/java/com/example/service/BonusAggregationService.java",
        qn="com.example.service.AggregatedBonusData",
        kind=UnitKind.TYPE,
        embedding=_emb(0, 1, 0),
    )
    fn = _unit(
        "map_fn",
        path="src/main/java/com/example/mapper/BonusResponseMapper.java",
        qn="com.example.mapper.BonusResponseMapper.mapToBonusResponseWithAggregation",
        embedding=_emb(0, 1, 0),
        parent_id=mapper.id,
        snippet=(
            "public BonusResponse mapToBonusResponseWithAggregation("
            "AggregatedBonusData aggregatedData) {\n"
            + "    return build(aggregatedData);\n" * 20
            + "}\n"
        ),
    )
    assert PlacementSensor().collect(_ctx([fn, mapper, data])) == []


def test_decision_rejects_cross_layer_mapper_move() -> None:
    from autorefactor.sensors.validate import decision_structural_reason

    mapper_fn = _unit(
        "map_fn2",
        path="src/main/java/com/example/mapper/BonusResponseMapper.java",
        qn="com.example.mapper.BonusResponseMapper.mapToBonusResponseWithAggregation",
        snippet="public BonusResponse map(AggregatedBonusData d) { return null; }\n",
    )
    data = _unit(
        "agg_data2",
        path="src/main/java/com/example/service/BonusAggregationService.java",
        qn="com.example.service.AggregatedBonusData",
        kind=UnitKind.TYPE,
    )
    units = {mapper_fn.id: mapper_fn, data.id: data}
    reason = decision_structural_reason(
        {
            "valid": True,
            "action": "move",
            "unit_ids": [mapper_fn.id, data.id],
        },
        set(units),
        units,
    )
    assert reason == "cross_layer_placement"


def test_placement_skips_interface_declared_method_on_impl() -> None:
    """Interface method names parsed from TYPE snippet (no surviving FUNCTION child)."""
    iface = _unit(
        "iface",
        path="src/main/java/BonusService.java",
        qn="com.example.BonusService",
        kind=UnitKind.TYPE,
        snippet="public interface BonusService {\n    Bonus getBonusFromPamResponse(PamResponse r);\n}\n",
        embedding=_emb(0, 1, 0),
    )
    impl = _unit(
        "impl",
        path="src/main/java/BonusServiceImpl.java",
        qn="com.example.BonusServiceImpl",
        kind=UnitKind.TYPE,
        embedding=_emb(0, 0.9, 0.1),
    )
    # No FUNCTION child on the interface — filter_units would drop short get* stubs.
    fn = _unit(
        "fn",
        path="src/main/java/BonusServiceImpl.java",
        qn="com.example.BonusServiceImpl.getBonusFromPamResponse",
        embedding=_emb(0, 1, 0),
        parent_id=impl.id,
        snippet=(
            "public Bonus getBonusFromPamResponse(PamResponse r) {\n"
            + "    return mapper.map(r);\n" * 20
            + "}\n"
        ),
    )
    findings = PlacementSensor().collect(_ctx([iface, impl, fn]))
    assert findings == []


def test_same_file_similarity_merge() -> None:
    shared = (
        "void processItem(Entity e) {\n"
        "    if (e == null) { return; }\n"
        "    Entity saved = repository.save(e);\n"
        "    cache.put(saved.getId(), saved);\n"
        "    log.info(\"saved {}\", saved.getId());\n"
        "}\n"
    )
    a = _unit(
        "procA",
        path="src/main/java/Svc.java",
        qn="Svc.processBonusBalance",
        snippet=shared,
        embedding=_emb(1, 0.02, 0),
        start=10,
        end=40,
    )
    b = _unit(
        "procB",
        path="src/main/java/Svc.java",
        qn="Svc.processBonusDispersion",
        snippet=shared.replace("processItem", "processDisp").replace("Entity", "Item"),
        embedding=_emb(1, 0.01, 0),
        start=50,
        end=80,
    )
    findings = emit_similarity_findings(
        [a, b],
        kinds={UnitKind.FUNCTION},
        goal=AnalysisGoal.METHOD_SIMILARITY,
        sensor="sim.method",
        threshold=0.78,
        max_pairs=10,
    )
    assert len(findings) == 1
    assert findings[0].action_hint == SuggestedAction.MERGE
    assert a.id in findings[0].unit_ids and b.id in findings[0].unit_ids


def test_same_file_shape_clone_same_parent_without_high_cosine() -> None:
    """Sibling methods with shape overlap should extract even if embedding cosine is mediocre."""
    a_src = (
        "private void processBonusBalance(Balance balance, String account) {\n"
        "    Long productId = mapping.mapToProductId(balance.getAssetType());\n"
        "    if (productId == null) { logger.warn(\"x\"); return; }\n"
        "    Long amount = balance.getAmount() != null ? balance.getAmount().getAmount() : 0L;\n"
        "    if (amount == 0) { return; }\n"
        "    MovementEntity movement = new MovementEntity();\n"
        "    movement.setAccountCode(account);\n"
        "    movement.setAmount(amount);\n"
        "    movement.setProductId(productId);\n"
        "    movement.setProcessed(false);\n"
        "    movementRepository.save(movement);\n"
        "    updateAggregation(account, productId, amount, SOURCE);\n"
        "}\n"
    )
    b_src = (
        "private void processDispersion(Dispersion dispersion, String account) {\n"
        "    Long productId = mapping.mapToProductId(dispersion.getType());\n"
        "    if (productId == null) { logger.warn(\"y\"); return; }\n"
        "    Long amount = dispersion.getAmount() != null ? dispersion.getAmount().getAmount() : 0L;\n"
        "    if (amount == 0) { return; }\n"
        "    Optional<MovementEntity> existing = movementRepository.findByTransactionId(tx);\n"
        "    if (existing.isPresent()) { return; }\n"
        "    MovementEntity movement = new MovementEntity();\n"
        "    movement.setAccountCode(account);\n"
        "    movement.setAmount(amount);\n"
        "    movement.setProductId(productId);\n"
        "    movement.setProcessed(false);\n"
        "    movementRepository.save(movement);\n"
        "    updateAggregation(account, productId, amount, SOURCE);\n"
        "}\n"
    )
    parent = _unit("par", path="src/main/java/Agg.java", qn="Agg", kind=UnitKind.TYPE, embedding=_emb(0, 1, 0))
    a = _unit(
        "lowA",
        path="src/main/java/Agg.java",
        qn="Agg.processBonusBalance",
        snippet=a_src,
        embedding=_emb(1, 0, 0),  # orthogonal-ish to b
        start=10,
        end=40,
        parent_id=parent.id,
    )
    b = _unit(
        "lowB",
        path="src/main/java/Agg.java",
        qn="Agg.processDispersion",
        snippet=b_src,
        embedding=_emb(0, 0, 1),
        start=50,
        end=85,
        parent_id=parent.id,
    )
    findings = emit_similarity_findings(
        [a, b, parent],
        kinds={UnitKind.FUNCTION},
        goal=AnalysisGoal.METHOD_SIMILARITY,
        sensor="sim.method",
        threshold=0.78,
        max_pairs=10,
    )
    assert len(findings) == 1
    assert findings[0].action_hint == SuggestedAction.EXTRACT_SHARED


def test_same_file_shape_clone_extracts_shared() -> None:
    """Renamed identifiers but same control-flow skeleton → EXTRACT_SHARED."""
    a_src = (
        "private void processBonusBalance(Balance balance, String account) {\n"
        "    Long productId = mapping.mapToProductId(balance.getAssetType());\n"
        "    if (productId == null) {\n"
        "        logger.warn(\"Cannot process balance: {}\", balance.getAssetType());\n"
        "        return;\n"
        "    }\n"
        "    Long amount = balance.getAmount() != null ? balance.getAmount().getAmount() : 0L;\n"
        "    if (amount == 0) { return; }\n"
        "    MovementEntity movement = new MovementEntity();\n"
        "    movement.setUserId(userId);\n"
        "    movement.setAccountCode(account);\n"
        "    movement.setAmount(amount);\n"
        "    movement.setProductId(productId);\n"
        "    movement.setProcessed(false);\n"
        "    movement.setSource(SOURCE);\n"
        "    movementRepository.save(movement);\n"
        "    updateAggregation(account, productId, amount, SOURCE);\n"
        "}\n"
    )
    b_src = (
        "private void processDispersion(Dispersion dispersion, String account) {\n"
        "    Long productId = mapping.mapToProductId(dispersion.getType());\n"
        "    if (productId == null) {\n"
        "        logger.warn(\"Cannot process dispersion: {}\", dispersion.getType());\n"
        "        return;\n"
        "    }\n"
        "    Long amount = dispersion.getAmount() != null ? dispersion.getAmount().getAmount() : 0L;\n"
        "    if (amount == 0) { return; }\n"
        "    MovementEntity movement = new MovementEntity();\n"
        "    movement.setUserId(userId);\n"
        "    movement.setAccountCode(account);\n"
        "    movement.setAmount(amount);\n"
        "    movement.setProductId(productId);\n"
        "    movement.setProcessed(false);\n"
        "    movement.setSource(SOURCE);\n"
        "    movementRepository.save(movement);\n"
        "    updateAggregation(account, productId, amount, SOURCE);\n"
        "}\n"
    )
    a = _unit(
        "shapeA",
        path="src/main/java/Agg.java",
        qn="Agg.processBonusBalance",
        snippet=a_src,
        embedding=_emb(1, 0.05, 0),
        start=10,
        end=40,
    )
    b = _unit(
        "shapeB",
        path="src/main/java/Agg.java",
        qn="Agg.processDispersion",
        snippet=b_src,
        embedding=_emb(1, 0.04, 0),
        start=50,
        end=80,
    )
    from autorefactor.sensors.similarity import shape_similarity

    assert shape_similarity(a_src, b_src) >= 0.65
    findings = emit_similarity_findings(
        [a, b],
        kinds={UnitKind.FUNCTION},
        goal=AnalysisGoal.METHOD_SIMILARITY,
        sensor="sim.method",
        threshold=0.78,
        max_pairs=10,
    )
    assert len(findings) == 1
    assert findings[0].action_hint == SuggestedAction.EXTRACT_SHARED
    assert "Structural clone" in findings[0].title


def test_sim_skips_test_paths_unless_include_tests() -> None:
    shared = (
        "void computeHmacMissing() {\n"
        + "    assertThrows(IllegalStateException.class, () -> svc.compute());\n" * 8
        + "}\n"
    )
    a = _unit(
        "t1",
        path="src/test/java/ATest.java",
        qn="ATest.computeHmac_missingSecret_throws",
        snippet=shared,
        embedding=_emb(1, 0, 0),
        start=1,
        end=20,
    )
    b = _unit(
        "t2",
        path="src/test/java/ATest.java",
        qn="ATest.computeHmac_nullSecret_throws",
        snippet=shared.replace("missing", "null"),
        embedding=_emb(0.99, 0.1, 0),
        start=21,
        end=40,
    )
    assert (
        emit_similarity_findings(
            [a, b],
            kinds={UnitKind.FUNCTION},
            goal=AnalysisGoal.METHOD_SIMILARITY,
            sensor="sim.method",
            threshold=0.78,
            max_pairs=10,
            include_tests=False,
        )
        == []
    )
    assert (
        len(
            emit_similarity_findings(
                [a, b],
                kinds={UnitKind.FUNCTION},
                goal=AnalysisGoal.METHOD_SIMILARITY,
                sensor="sim.method",
                threshold=0.78,
                max_pairs=10,
                include_tests=True,
            )
        )
        >= 1
    )


def test_wiring_orphan_kafka(tmp_path: Path) -> None:
    from autorefactor.sensors.wiring import WiringSensor

    repo = tmp_path / "svc"
    (repo / "src/main/resources").mkdir(parents=True)
    (repo / "pom.xml").write_text(
        "<project><dependencies><dependency>"
        "<groupId>org.springframework.kafka</groupId>"
        "<artifactId>spring-kafka</artifactId>"
        "</dependency></dependencies></project>",
        encoding="utf-8",
    )
    (repo / "src/main/resources/application.yml").write_text(
        "psn:\n  bootstrap.server: localhost:9092\n  topics.transaction: t1\n",
        encoding="utf-8",
    )
    unit = _unit("repo", path=".", qn="svc", kind=UnitKind.REPO, snippet="repo")
    unit = unit.model_copy(update={"repo": "svc"})
    prod = _unit(
        "svc-type",
        path="src/main/java/com/example/TxnService.java",
        qn="com.example.TxnService",
        kind=UnitKind.TYPE,
        repo="svc",
    )
    ctx = AnalysisContext(
        workspace=tmp_path,
        repos=(repo,),
        units=(unit, prod),
        config=AutorefactorConfig(skip_llm=True, workspace=tmp_path),
        focus="balanced",
    )
    findings = WiringSensor().collect(ctx)
    kafka = [f for f in findings if (f.metadata or {}).get("kind") == "orphan_kafka"]
    assert kafka
    assert kafka[0].action_hint.value == "document"
    assert "wire_kafka_consumer" in (kafka[0].evidence or "")
    assert "remove_dead_wiring" in (kafka[0].evidence or "")
    assert kafka[0].unit_ids == [prod.id]
    assert (kafka[0].metadata or {}).get("path") not in {".", "svc", ""}


def test_wiring_grpc_entry_ensure_tests(tmp_path: Path) -> None:
    from autorefactor.domain import SuggestedAction
    from autorefactor.sensors.wiring import WiringSensor

    repo = tmp_path / "bonus"
    src = repo / "src/main/java/com/example"
    src.mkdir(parents=True)
    (repo / "pom.xml").write_text("<project></project>", encoding="utf-8")
    (src / "GetBonusGrpcService.java").write_text(
        "public class GetBonusGrpcService extends BonusGrpc.BonusImplBase {\n"
        "  public void getBonus() {}\n"
        "}\n",
        encoding="utf-8",
    )
    grpc = _unit(
        "grpc",
        path="src/main/java/com/example/GetBonusGrpcService.java",
        qn="com.example.GetBonusGrpcService",
        kind=UnitKind.TYPE,
        repo="bonus",
        snippet="public class GetBonusGrpcService extends BonusGrpc.BonusImplBase {}",
    )
    ctx = AnalysisContext(
        workspace=tmp_path,
        repos=(repo,),
        units=(grpc,),
        config=AutorefactorConfig(skip_llm=True, workspace=tmp_path),
        focus="balanced",
    )
    findings = WiringSensor().collect(ctx)
    grpc_f = [f for f in findings if (f.metadata or {}).get("kind") == "grpc_entry"]
    assert grpc_f
    assert grpc_f[0].action_hint == SuggestedAction.ENSURE_TESTS
    assert grpc_f[0].unit_ids == [grpc.id]


def test_sibling_structure_findings_conflict_linked() -> None:
    from autorefactor.agenda import conflict_components
    from autorefactor.domain import Finding, Severity

    parent = _unit("P", path="src/main/java/M.java", qn="M", kind=UnitKind.TYPE)
    a = _unit("fa", path="src/main/java/M.java", qn="M.mergeA", parent_id=parent.id, start=1, end=60)
    b = _unit("fb", path="src/main/java/M.java", qn="M.mergeB", parent_id=parent.id, start=70, end=130)
    f1 = Finding(
        id="1",
        goal=AnalysisGoal.STRUCTURE,
        sensor="structure",
        severity=Severity.MEDIUM,
        title="Long function M.mergeA",
        evidence="60 lines",
        unit_ids=[a.id],
        action_hint=SuggestedAction.SPLIT,
    )
    f2 = Finding(
        id="2",
        goal=AnalysisGoal.STRUCTURE,
        sensor="structure",
        severity=Severity.MEDIUM,
        title="Long function M.mergeB",
        evidence="60 lines",
        unit_ids=[b.id],
        action_hint=SuggestedAction.SPLIT,
    )
    comps = conflict_components([f1, f2], {parent.id: parent, a.id: a, b.id: b})
    assert len(comps) == 1
    assert {f.id for f in comps[0]} == {"1", "2"}


def test_structure_long_function_threshold_50_skips_tests() -> None:
    cfg = AutorefactorConfig(skip_llm=True, long_function_lines=50, large_file_lines=280)
    long_main = _unit(
        "long",
        path="src/main/java/Svc.java",
        qn="Svc.getBonus",
        start=1,
        end=51,
        snippet="void getBonus() {\n" + "    x();\n" * 50 + "}\n",
    )
    short_main = _unit(
        "short",
        path="src/main/java/Svc.java",
        qn="Svc.tiny",
        start=1,
        end=49,
        snippet="void tiny() {\n" + "    x();\n" * 48 + "}\n",
    )
    long_test = _unit(
        "testlong",
        path="src/test/java/SvcTest.java",
        qn="SvcTest.big",
        start=1,
        end=60,
        snippet="void big() {\n" + "    x();\n" * 59 + "}\n",
    )
    large_file = _unit(
        "file",
        path="src/main/java/Mapper.java",
        qn="Mapper.java",
        kind=UnitKind.FILE,
        start=1,
        end=280,
        snippet="// file\n" * 280,
    )
    findings = StructureSensor().collect(
        AnalysisContext(
            workspace=Path("/tmp"),
            repos=(Path("/tmp/demo"),),
            units=tuple([long_main, short_main, long_test, large_file]),
            config=cfg,
            focus="balanced",
        )
    )
    titles = [f.title for f in findings]
    assert any("Svc.getBonus" in t for t in titles)
    assert not any("Svc.tiny" in t for t in titles)
    assert not any("SvcTest.big" in t for t in titles)
    # Size alone is insufficient — cohesive single-type files need a second signal
    assert not any("Mapper.java" in t for t in titles)


def test_config_defaults_structure() -> None:
    cfg = AutorefactorConfig(skip_llm=True)
    assert cfg.long_function_lines == 50
    assert cfg.large_file_lines == 280


def test_coverage_skips_application_and_exceptions_prefers_file() -> None:
    app_file = _unit(
        "appf",
        path="src/main/java/com/demo/DemoApplication.java",
        qn="com.demo.DemoApplication",
        kind=UnitKind.FILE,
        start=1,
        end=20,
    )
    app_mod = _unit(
        "appm",
        path="src/main/java/com/demo/DemoApplication.java",
        qn="com.demo",
        kind=UnitKind.MODULE,
        start=1,
        end=20,
    )
    exc_file = _unit(
        "excf",
        path="src/main/java/com/demo/BoomException.java",
        qn="com.demo.BoomException",
        kind=UnitKind.FILE,
        start=1,
        end=30,
    )
    svc_file = _unit(
        "svcf",
        path="src/main/java/com/demo/BonusService.java",
        qn="com.demo.BonusService",
        kind=UnitKind.FILE,
        start=1,
        end=80,
    )
    snap = CoverageSnapshot(
        summary="test",
        overall_percent=50.0,
        source="jacoco",
        files=[
            CoverageFileStat(
                path="com/demo/DemoApplication.java",
                covered_lines=1,
                total_lines=10,
                percent=10.0,
            ),
            CoverageFileStat(
                path="com/demo/BoomException.java",
                covered_lines=5,
                total_lines=10,
                percent=50.0,
            ),
            CoverageFileStat(
                path="com/demo/BonusService.java",
                covered_lines=0,
                total_lines=80,
                percent=0.0,
            ),
            CoverageFileStat(
                path="com/demo/Tiny.java",
                covered_lines=0,
                total_lines=5,
                percent=0.0,
            ),
            CoverageFileStat(
                path="com/demo/OpenApiConfig.java",
                covered_lines=0,
                total_lines=25,
                percent=0.0,
            ),
            CoverageFileStat(
                path="com/demo/mapper/FooMapperImpl.java",
                covered_lines=0,
                total_lines=100,
                percent=0.0,
            ),
            CoverageFileStat(
                path="com/demo/dto/Item.java",
                covered_lines=0,
                total_lines=40,
                percent=0.0,
            ),
        ],
    )
    ctx = AnalysisContext(
        workspace=Path("/tmp"),
        repos=(Path("/tmp/demo"),),
        units=tuple([app_file, app_mod, exc_file, svc_file]),
        config=AutorefactorConfig(skip_llm=True, coverage_threshold=80.0),
        focus="balanced",
        coverage=snap,
    )
    findings = CoverageSensor().collect(ctx)
    titles = [f.title for f in findings]
    assert not any("DemoApplication" in t for t in titles)
    assert not any("BoomException" in t for t in titles)
    assert not any("Tiny" in t for t in titles)
    assert not any("OpenApiConfig" in t for t in titles)
    assert not any("MapperImpl" in t for t in titles)
    assert not any("dto/Item" in t for t in titles)
    assert len(findings) == 1
    assert "BonusService" in findings[0].title
    assert findings[0].unit_ids == [svc_file.id]


def test_coverage_ranks_by_uncovered_and_caps_per_repo() -> None:
    big = _unit(
        "big",
        path="src/main/java/com/demo/BigService.java",
        qn="com.demo.BigService",
        kind=UnitKind.FILE,
        start=1,
        end=200,
    )
    mid = _unit(
        "mid",
        path="src/main/java/com/demo/MidService.java",
        qn="com.demo.MidService",
        kind=UnitKind.FILE,
        start=1,
        end=50,
    )
    small = _unit(
        "small",
        path="src/main/java/com/demo/SmallService.java",
        qn="com.demo.SmallService",
        kind=UnitKind.FILE,
        start=1,
        end=20,
    )
    # Many near-zero files — only top-2 by uncovered LOC should survive
    files = [
        CoverageFileStat(path="com/demo/BigService.java", covered_lines=1, total_lines=200, percent=0.5),
        CoverageFileStat(path="com/demo/MidService.java", covered_lines=0, total_lines=50, percent=0.0),
        CoverageFileStat(path="com/demo/SmallService.java", covered_lines=0, total_lines=20, percent=0.0),
    ]
    for i in range(10):
        files.append(
            CoverageFileStat(
                path=f"com/demo/Handler{i}.java",
                covered_lines=0,
                total_lines=16,
                percent=0.0,
            )
        )
    units = [big, mid, small] + [
        _unit(
            f"h{i}",
            path=f"src/main/java/com/demo/Handler{i}.java",
            qn=f"com.demo.Handler{i}",
            kind=UnitKind.FILE,
            start=1,
            end=20,
        )
        for i in range(10)
    ]
    ctx = AnalysisContext(
        workspace=Path("/tmp"),
        repos=(Path("/tmp/demo"),),
        units=tuple(units),
        config=AutorefactorConfig(skip_llm=True, coverage_threshold=80.0, max_coverage_findings=2),
        focus="balanced",
        coverage=CoverageSnapshot(summary="t", overall_percent=10.0, source="jacoco", files=files),
    )
    findings = CoverageSensor().collect(ctx)
    assert len(findings) == 2
    titles = [f.title for f in findings]
    assert any("BigService" in t for t in titles)
    assert any("MidService" in t for t in titles)
    assert not any("SmallService" in t for t in titles)
    assert not any("Handler0" in t for t in titles)
    big_f = next(f for f in findings if "BigService" in f.title)
    mid_f = next(f for f in findings if "MidService" in f.title)
    assert big_f.score > mid_f.score
    assert big_f.metrics["uncovered_lines"] > mid_f.metrics["uncovered_lines"]


def test_extract_shared_loc_requires_two_units() -> None:
    a = _unit("a", path="A.java", qn="A.foo", start=1, end=20)
    b = _unit("b", path="B.java", qn="B.foo", start=1, end=30)
    by_id = {a.id: a, b.id: b}
    assert _estimate_loc_delta(SuggestedAction.EXTRACT_SHARED.value, [a.id], by_id) == 0
    assert _estimate_loc_delta(SuggestedAction.EXTRACT_SHARED.value, [a.id, b.id], by_id) == 10


def test_missing_coverage_skipped_when_jacoco_in_pom(tmp_path: Path) -> None:
    from autorefactor.sensors.coverage import CoverageSensor, coverage_tool_configured

    repo = tmp_path / "bonus"
    repo.mkdir()
    (repo / "pom.xml").write_text(
        "<project><build><plugins><plugin>"
        "<artifactId>jacoco-maven-plugin</artifactId>"
        "</plugin></plugins></build></project>",
        encoding="utf-8",
    )
    assert coverage_tool_configured(repo) is True
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "pom.xml").write_text("<project></project>", encoding="utf-8")
    assert coverage_tool_configured(bare) is False

    repo_unit = _unit("ru", path=".", qn="bonus", kind=UnitKind.REPO, repo="bonus")
    prod = _unit(
        "bonus-svc",
        path="src/main/java/com/example/BonusService.java",
        qn="com.example.BonusService",
        kind=UnitKind.TYPE,
        repo="bonus",
    )
    bare_unit = _unit("bu", path=".", qn="bare", kind=UnitKind.REPO, repo="bare")
    cfg = AutorefactorConfig(skip_llm=True, workspace=tmp_path)
    ctx = AnalysisContext(
        workspace=tmp_path,
        repos=(repo, bare),
        units=(repo_unit, prod, bare_unit),
        config=cfg,
        focus="balanced",
        coverage=CoverageSnapshot(
            summary="missing",
            overall_percent=None,
            files=[],
            source="missing",
            repos_without_coverage=["bonus", "bare"],
        ),
    )
    findings = CoverageSensor().collect(ctx)
    titles = [f.title for f in findings]
    from autorefactor.sensors.anchors import is_repo_root_path

    estimated = [f for f in findings if (f.metadata or {}).get("kind") == "coverage_estimated"]
    assert estimated
    assert estimated[0].unit_ids == [prod.id]
    assert not is_repo_root_path(str(estimated[0].metadata.get("path") or ""), "bonus")
    assert any("bare" in t for t in titles)


def test_missing_coverage_skipped_when_jacoco_in_nested_pom(tmp_path: Path) -> None:
    from autorefactor.sensors.coverage import CoverageSensor, coverage_tool_configured

    repo = tmp_path / "parent"
    module = repo / "module-a"
    module.mkdir(parents=True)
    (repo / "pom.xml").write_text("<project><modules><module>module-a</module></modules></project>", encoding="utf-8")
    (module / "pom.xml").write_text(
        "<project><build><plugins><plugin>"
        "<artifactId>jacoco-maven-plugin</artifactId>"
        "</plugin></plugins></build></project>",
        encoding="utf-8",
    )
    assert coverage_tool_configured(repo) is True

    repo_unit = _unit("ru", path=".", qn="parent", kind=UnitKind.REPO, repo="parent")
    cfg = AutorefactorConfig(skip_llm=True, workspace=tmp_path)
    ctx = AnalysisContext(
        workspace=tmp_path,
        repos=(repo,),
        units=(repo_unit,),
        config=cfg,
        focus="balanced",
        coverage=CoverageSnapshot(
            summary="missing",
            overall_percent=None,
            files=[],
            source="missing",
            repos_without_coverage=["parent"],
        ),
    )
    findings = CoverageSensor().collect(ctx)
    assert findings == []


def test_large_file_requires_second_signal() -> None:
    cfg = AutorefactorConfig(skip_llm=True, large_file_lines=280)
    cohesive = _unit(
        "cohesive",
        path="src/main/java/Mapper.java",
        qn="Mapper.java",
        kind=UnitKind.FILE,
        start=1,
        end=300,
        snippet="// line\n" * 300,
    )
    busy_file = _unit(
        "busy",
        path="src/main/java/Fat.java",
        qn="Fat.java",
        kind=UnitKind.FILE,
        start=1,
        end=300,
        snippet="// line\n" * 300,
    )
    kids = [
        _unit(
            f"m{i}",
            path="src/main/java/Fat.java",
            qn=f"Fat.m{i}",
            start=i * 10,
            end=i * 10 + 5,
            snippet="void m() { return; }\n",
        )
        for i in range(15)
    ]
    findings = StructureSensor().collect(
        AnalysisContext(
            workspace=Path("/tmp"),
            repos=(Path("/tmp/demo"),),
            units=tuple([cohesive, busy_file, *kids]),
            config=cfg,
            focus="balanced",
        )
    )
    titles = [f.title for f in findings]
    assert not any("Mapper.java" in t for t in titles)
    assert any("Fat.java" in t for t in titles)


def test_boilerplate_dto_converter_not_flagged_long() -> None:
    from autorefactor.sensors.complexity import ComplexitySensor

    cfg = AutorefactorConfig(skip_llm=True, long_function_lines=50)
    converter = _unit(
        "conv",
        path="src/main/java/Dto.java",
        qn="Dto.mapToBonusResponse",
        start=1,
        end=60,
        snippet="void mapToBonusResponse() {\n" + "    x();\n" * 58 + "}\n",
    )
    findings = StructureSensor().collect(
        AnalysisContext(
            workspace=Path("/tmp"),
            repos=(Path("/tmp/demo"),),
            units=(converter,),
            config=cfg,
            focus="balanced",
        )
    )
    assert findings == []
    # Nested catch-heavy HTTP handlers also suppressed by complexity sensor
    catch_heavy = _unit(
        "http",
        path="src/main/java/Api.java",
        qn="Api.handleRequest",
        start=1,
        end=50,
        snippet=(
            "void handleRequest() {\n"
            + "\n".join(f"    int v{i} = {i};" for i in range(20))
            + "\n    try { work(); } catch (IOException e) { log(e); }\n"
            "    try { work2(); } catch (SQLException e) { log(e); }\n"
            "    try { work3(); } catch (RuntimeException e) { log(e); }\n"
            "    if (a) { if (b) { if (c) { if (d) { doIt(); } } } }\n"
            "}\n"
        ),
    )
    cx_findings = ComplexitySensor().collect(
        AnalysisContext(
            workspace=Path("/tmp"),
            repos=(Path("/tmp/demo"),),
            units=(converter, catch_heavy),
            config=cfg,
            focus="balanced",
        )
    )
    assert not any("mapToBonusResponse" in f.title for f in cx_findings)


def test_domain_divergence_skips_extract_shared() -> None:
    from autorefactor.sensors.similarity import domain_token_divergence, emit_similarity_findings

    a_src = (
        "void processBonusBalance() {\n"
        "  String syntheticTxId = UUID.randomUUID().toString();\n"
        "  bonusBalanceRepository.save(syntheticTxId, assetId);\n"
        "  notifyBonusBalanceChanged(syntheticTxId);\n"
        "}\n"
    )
    b_src = (
        "void processDispersion() {\n"
        "  String idempotentKey = request.getIdempotencyKey();\n"
        "  dispersionRepository.saveIdempotent(idempotentKey, productId);\n"
        "  notifyDispersionCompleted(idempotentKey);\n"
        "}\n"
    )
    assert domain_token_divergence(a_src, b_src) >= 0.45
    emb = _emb(1, 0, 0)
    a = _unit(
        "bal",
        path="Svc.java",
        qn="Svc.processBonusBalance",
        snippet=a_src,
        start=1,
        end=20,
        embedding=emb,
        parent_id="p",
    )
    b = _unit(
        "dis",
        path="Svc.java",
        qn="Svc.processDispersion",
        snippet=b_src,
        start=30,
        end=50,
        embedding=emb,
        parent_id="p",
    )
    findings = emit_similarity_findings(
        [a, b],
        kinds={UnitKind.FUNCTION},
        goal=AnalysisGoal.METHOD_SIMILARITY,
        sensor="sim.method",
        threshold=0.5,
        max_pairs=10,
    )
    assert not any(f.action_hint == SuggestedAction.EXTRACT_SHARED for f in findings)

    twin = _unit(
        "twin",
        path="Other.java",
        qn="Other.processBonusBalance",
        snippet=a_src,
        start=1,
        end=20,
        embedding=emb,
        repo="other",
    )
    twin_findings = emit_similarity_findings(
        [a, twin],
        kinds={UnitKind.FUNCTION},
        goal=AnalysisGoal.METHOD_SIMILARITY,
        sensor="sim.method",
        threshold=0.5,
        max_pairs=10,
    )
    assert any(
        f.action_hint
        in {SuggestedAction.EXTRACT_SHARED, SuggestedAction.DELETE_DUPLICATE}
        for f in twin_findings
    )
