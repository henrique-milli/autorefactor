from __future__ import annotations

from pathlib import Path

from autorefactor.agenda import conflict_components
from autorefactor.changesets import Changeset, ChangesetStatus, ChangesetStore
from autorefactor.config_store import get_setup_requirements, load_toml, save_toml
from autorefactor.domain import AnalysisGoal, Finding, Severity, SuggestedAction
from autorefactor.mcp.server import dispatch
from autorefactor.report.metrics import estimate_metrics


def test_no_refactor_command() -> None:
    from autorefactor.cli import app

    names = {cmd.name for cmd in app.registered_commands}
    assert "refactor" not in names
    assert "analyze" in names
    assert "init" in names


def test_changeset_store_confidence_gate(tmp_path: Path) -> None:
    store = ChangesetStore(tmp_path / "queue.json")
    store.save(
        [
            Changeset(
                id="a",
                title="low",
                action="merge",
                brief="b",
                rationale="r",
                confidence=0.3,
            ),
            Changeset(
                id="b",
                title="high",
                action="split",
                brief="b2",
                rationale="r2",
                confidence=0.9,
            ),
        ]
    )
    nxt = store.next_pending(min_confidence=0.5)
    assert nxt.changeset is not None
    assert nxt.changeset.id == "b"
    assert not nxt.needs_confirmation
    store.mark("b", ChangesetStatus.DONE)
    nxt2 = store.next_pending(min_confidence=0.5)
    assert nxt2.changeset is not None
    assert nxt2.changeset.id == "a"
    assert nxt2.needs_confirmation


def test_delete_duplicate_at_threshold_requires_hitl(tmp_path: Path) -> None:
    store = ChangesetStore(tmp_path / "queue.json")
    store.save(
        [
            Changeset(
                id="de9cd8bdb03b74c9",
                title="Delete duplicate @ExceptionHandler",
                action="delete_duplicate",
                brief="Merge BonusExceptionHandler.handleExternalClientException handlers",
                rationale="slop dedup: handler merge for @ExceptionHandler",
                confidence=0.60,
                evidence_refs=["slop:handler:u1"],
            ),
            Changeset(
                id="split1",
                title="Split mergeAggregations",
                action="split",
                brief="b",
                rationale="r",
                confidence=0.54,
            ),
        ]
    )
    threshold = 0.60
    nxt = store.next_pending(min_confidence=threshold)
    assert nxt.changeset is not None
    assert nxt.changeset.id == "de9cd8bdb03b74c9"
    assert nxt.needs_confirmation is True

    store.mark("de9cd8bdb03b74c9", ChangesetStatus.SKIPPED)
    nxt2 = store.next_pending(min_confidence=threshold)
    assert nxt2.changeset is not None
    assert nxt2.changeset.id == "split1"
    assert nxt2.needs_confirmation is True


def test_high_confidence_ensure_tests_autoruns(tmp_path: Path) -> None:
    from autorefactor.changesets.store import ChangesetStore, ensure_tests_padding_hitl

    cs = Changeset(
        id="mock-only",
        title="Ensure tests for PamClientException",
        action="ensure_tests",
        brief="Characterization coverage for BonusExceptionHandler.",
        rationale="prep",
        confidence=0.90,
        metrics_delta={"kind": "test_quality"},
    )
    assert ensure_tests_padding_hitl(cs) is False
    assert ensure_tests_padding_hitl(cs, 0.6) is False

    store = ChangesetStore(tmp_path / "queue.json")
    store.save([cs])
    nxt = store.next_pending(min_confidence=0.60)
    assert nxt.changeset is not None
    assert nxt.changeset.id == "mock-only"
    assert nxt.needs_confirmation is False


def test_low_confidence_ensure_tests_requires_hitl(tmp_path: Path) -> None:
    from autorefactor.changesets.store import ChangesetStore, ensure_tests_padding_hitl

    cs = Changeset(
        id="low-prep",
        title="Ensure tests for unknown unit",
        action="ensure_tests",
        brief="Ask the user before applying (HITL).",
        rationale="weak prep",
        confidence=0.40,
    )
    assert ensure_tests_padding_hitl(cs) is True

    store = ChangesetStore(tmp_path / "queue.json")
    store.save([cs])
    nxt = store.next_pending(min_confidence=0.60)
    assert nxt.changeset is not None
    assert nxt.needs_confirmation is True
    assert nxt.hitl_reason == "low_confidence"


def test_split_at_threshold_auto_proceeds(tmp_path: Path) -> None:
    store = ChangesetStore(tmp_path / "queue.json")
    store.save(
        [
            Changeset(
                id="split-at-band",
                title="Split service",
                action="split",
                brief="b",
                rationale="r",
                confidence=0.60,
            ),
        ]
    )
    nxt = store.next_pending(min_confidence=0.60)
    assert nxt.changeset is not None
    assert nxt.changeset.id == "split-at-band"
    assert not nxt.needs_confirmation


def test_conflict_components_merge_shared_units() -> None:
    f1 = Finding(
        id="1",
        goal=AnalysisGoal.STRUCTURE, sensor='structure',
        severity=Severity.MEDIUM,
        title="t1",
        evidence="e",
        unit_ids=["u1", "u2"],
        action_hint=SuggestedAction.MOVE,
    )
    f2 = Finding(
        id="2",
        goal=AnalysisGoal.METHOD_SIMILARITY, sensor='sim.method',
        severity=Severity.HIGH,
        title="t2",
        evidence="e",
        unit_ids=["u2", "u3"],
        action_hint=SuggestedAction.MERGE,
    )
    f3 = Finding(
        id="3",
        goal=AnalysisGoal.STRUCTURE, sensor='structure',
        severity=Severity.LOW,
        title="t3",
        evidence="e",
        unit_ids=["u9"],
        action_hint=SuggestedAction.SPLIT,
    )
    comps = conflict_components([f1, f2, f3])
    assert len(comps) == 2
    sizes = sorted(len(c) for c in comps)
    assert sizes == [1, 2]


def test_toml_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "c.toml"
    save_toml(path, {"llm_model": "x", "confidence_threshold": 0.7})
    data = load_toml(path)
    assert data["llm_model"] == "x"
    assert data["confidence_threshold"] == 0.7


def test_setup_requirements_without_key(monkeypatch) -> None:
    from autorefactor.config import AutorefactorConfig

    monkeypatch.delenv("AUTOREFACTOR_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    agent = AutorefactorConfig(
        _env_file=None,
        chat_backend="agent",
        embed_backend="local",
        skip_llm=False,
        api_key=None,
        llm_api_key=None,
    )
    assert get_setup_requirements(agent) == []
    cfg = AutorefactorConfig(
        _env_file=None,
        chat_backend="http",
        embed_backend="http",
        skip_llm=False,
        describe_mode="raw",
        api_key=None,
        llm_api_key=None,
    )
    reqs = get_setup_requirements(cfg)
    assert any(r["field"] == "api_key" for r in reqs)


def test_metrics_shape() -> None:
    metrics = estimate_metrics(units=[], findings=[], confirmed=[], changesets=[])
    assert "exact" in metrics and "estimates" in metrics and "scores" in metrics
    assert "impact" in metrics
    assert "headline" in metrics["impact"]
    assert "roi" in metrics
    assert "debt_score" in metrics["roi"]
    assert "agent_friction" in metrics["roi"]


def test_metrics_impact_highlights_review_and_cross_repo() -> None:
    from autorefactor.changesets.models import Changeset, ChangesetTarget
    from autorefactor.domain import CodeSpan, CodeUnit, UnitKind, content_hash
    from autorefactor.report.metrics import write_metrics_and_report

    units = [
        CodeUnit(
            id="u1",
            repo="alpha",
            path="a.java",
            qualified_name="A",
            kind=UnitKind.FILE,
            language="java",
            source_snippet="\n".join(["x"] * 100),
            content_hash=content_hash("a"),
            span=CodeSpan(start_line=1, end_line=100),
        ),
        CodeUnit(
            id="u2",
            repo="beta",
            path="b.java",
            qualified_name="B",
            kind=UnitKind.FILE,
            language="java",
            source_snippet="\n".join(["y"] * 50),
            content_hash=content_hash("b"),
            span=CodeSpan(start_line=1, end_line=50),
        ),
    ]
    cs = Changeset(
        id="c1",
        title="Extract shared util across repos",
        action="extract_shared",
        brief="do it",
        rationale="clone",
        confidence=0.95,
        targets=[
            ChangesetTarget(repo="alpha", path="a.java", symbol="A"),
            ChangesetTarget(repo="beta", path="b.java", symbol="B"),
        ],
        metrics_delta={"estimated_loc_removed": 10, "loc_under_review": 80, "goal": "structure"},
    )
    metrics = estimate_metrics(units=units, findings=[], confirmed=[], changesets=[cs])
    assert metrics["impact"]["cross_repo_opportunities"] == 1
    assert metrics["impact"]["loc_under_review"] == 80
    assert metrics["impact"]["dedup_share_delete_track"] == 1
    assert metrics["exact"]["cross_repo_changeset_count"] == 1
    assert "80" in metrics["impact"]["headline"] or "agent-ready" in metrics["impact"]["headline"]
    assert metrics["impact"]["pitch"]
    assert metrics["impact"]["upside_bullets"]
    assert any("Cross-repo" in b or "cross-repo" in b for b in metrics["impact"]["upside_bullets"])
    assert metrics["impact"]["estimated_loc_removed"] == 10
    assert "10" in metrics["impact"]["headline"] or "dedup" in metrics["impact"]["headline"]


def test_report_md_leads_with_refactor_upside(tmp_path: Path) -> None:
    from autorefactor.changesets.models import Changeset, ChangesetTarget
    from autorefactor.domain import CodeSpan, CodeUnit, UnitKind, content_hash
    from autorefactor.report.metrics import write_metrics_and_report

    units = [
        CodeUnit(
            id="u1",
            repo="alpha",
            path="a.java",
            qualified_name="A",
            kind=UnitKind.FILE,
            language="java",
            source_snippet="\n".join(["x"] * 40),
            content_hash=content_hash("a"),
            span=CodeSpan(start_line=1, end_line=40),
        )
    ]
    cs = Changeset(
        id="c1",
        title="Delete duplicate helper",
        action="delete_duplicate",
        brief="b",
        rationale="r",
        confidence=0.9,
        targets=[ChangesetTarget(repo="alpha", path="a.java", symbol="A")],
        metrics_delta={"estimated_loc_removed": 12, "loc_under_review": 24, "goal": "method_similarity"},
    )
    metrics = estimate_metrics(units=units, findings=[], confirmed=[], changesets=[cs])
    report_dir = tmp_path / "report"
    write_metrics_and_report(report_dir, metrics=metrics, changesets=[cs], visual_paths=[])
    md = (report_dir / "report.md").read_text(encoding="utf-8")
    assert md.index("Executive brief") < md.index("Scoreboard")
    assert "Agent economics" in md
    assert "Top opportunities" in md
    assert "estimate" in md.lower()


def test_mcp_dispatch_list_empty(tmp_path: Path) -> None:
    out = tmp_path / "out"
    (out / "changesets").mkdir(parents=True)
    ChangesetStore(out / "changesets" / "queue.json").save([])
    result = dispatch("list_changesets", {"out": str(out)})
    assert result["changesets"] == []


def test_reconcile_shared_units_one_component(tmp_path: Path) -> None:
    from autorefactor.models import CodeSpan, CodeUnit, UnitKind, content_hash, unit_id
    from autorefactor.providers import StubProvider
    from autorefactor.reconcile import reconcile_flags

    u1 = CodeUnit(
        id=unit_id("a"),
        repo="demo",
        path="a.py",
        qualified_name="a.f",
        kind=UnitKind.FUNCTION,
        language="python",
        source_snippet="def f():\n    return 1\n",
        content_hash=content_hash("a"),
        span=CodeSpan(start_line=1, end_line=2),
    )
    u2 = CodeUnit(
        id=unit_id("b"),
        repo="demo",
        path="b.py",
        qualified_name="b.f",
        kind=UnitKind.FUNCTION,
        language="python",
        source_snippet="def f():\n    return 1\n",
        content_hash=content_hash("b"),
        span=CodeSpan(start_line=1, end_line=2),
    )
    f1 = Finding(
        id="f1",
        goal=AnalysisGoal.STRUCTURE, sensor='structure',
        severity=Severity.MEDIUM,
        title="dup name",
        evidence="e",
        unit_ids=[u1.id, u2.id],
        action_hint=SuggestedAction.MERGE,
        score=0.8,
    )
    f2 = Finding(
        id="f2",
        goal=AnalysisGoal.METHOD_SIMILARITY, sensor='sim.method',
        severity=Severity.HIGH,
        title="similar",
        evidence="e",
        unit_ids=[u1.id, u2.id],
        action_hint=SuggestedAction.EXTRACT_SHARED,
        score=0.9,
    )
    findings, changesets = reconcile_flags([f1, f2], [u1, u2], StubProvider(), concurrency=1)
    assert findings
    assert changesets
    # Shared units → single conflict component → stub may emit one decision per flag;
    # all targets should reference the shared units.
    all_targets = {t.unit_id for cs in changesets for t in cs.targets}
    assert u1.id in all_targets and u2.id in all_targets


def test_analyze_skip_llm_writes_queue(tmp_path: Path) -> None:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures" / "demo-workspace"
    out = tmp_path / "out"
    from autorefactor.config import AutorefactorConfig
    from autorefactor.stages.analyze import run_analyze

    config = AutorefactorConfig(
        workspace=fixtures,
        out_dir=out,
        skip_llm=True,
        sensors="auto",
        describe_mode="raw",
    )
    artifacts = run_analyze(config)
    assert artifacts.findings is not None
    queue = out / "changesets" / "queue.json"
    assert queue.exists()
    assert (out / "report" / "report.md").exists()
