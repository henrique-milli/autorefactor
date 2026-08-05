from __future__ import annotations

from pathlib import Path

from coderazor.analyze.conflict_graph import conflict_components
from coderazor.changesets import Changeset, ChangesetStatus, ChangesetStore
from coderazor.config_store import get_setup_requirements, load_toml, save_toml
from coderazor.models import Flag, FlagSource, Severity, SuggestedAction
from coderazor.mcp.server import dispatch
from coderazor.report.metrics import estimate_metrics


def test_no_refactor_command() -> None:
    from coderazor.cli import app

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


def test_conflict_components_merge_shared_units() -> None:
    f1 = Flag(
        id="1",
        source=FlagSource.SMELL,
        severity=Severity.MEDIUM,
        title="t1",
        evidence="e",
        unit_ids=["u1", "u2"],
        suggested_action=SuggestedAction.MOVE,
    )
    f2 = Flag(
        id="2",
        source=FlagSource.SIMILARITY,
        severity=Severity.HIGH,
        title="t2",
        evidence="e",
        unit_ids=["u2", "u3"],
        suggested_action=SuggestedAction.MERGE,
    )
    f3 = Flag(
        id="3",
        source=FlagSource.SMELL,
        severity=Severity.LOW,
        title="t3",
        evidence="e",
        unit_ids=["u9"],
        suggested_action=SuggestedAction.SPLIT,
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
    from coderazor.config import CoderazorConfig

    cfg = CoderazorConfig(skip_llm=False, describe_mode="index", api_key=None, llm_api_key=None)
    monkeypatch.delenv("CODERAZOR_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    reqs = get_setup_requirements(cfg)
    assert any(r["field"] == "api_key" for r in reqs)


def test_metrics_shape() -> None:
    metrics = estimate_metrics(units=[], flags=[], findings=[], changesets=[])
    assert "exact" in metrics and "estimates" in metrics and "scores" in metrics


def test_mcp_dispatch_list_empty(tmp_path: Path) -> None:
    out = tmp_path / "out"
    (out / "changesets").mkdir(parents=True)
    ChangesetStore(out / "changesets" / "queue.json").save([])
    result = dispatch("list_changesets", {"out": str(out)})
    assert result["changesets"] == []


def test_reconcile_shared_units_one_component(tmp_path: Path) -> None:
    from coderazor.models import CodeSpan, CodeUnit, UnitKind, content_hash, unit_id
    from coderazor.providers import StubProvider
    from coderazor.reconcile import reconcile_flags

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
    f1 = Flag(
        id="f1",
        source=FlagSource.SMELL,
        severity=Severity.MEDIUM,
        title="dup name",
        evidence="e",
        unit_ids=[u1.id, u2.id],
        suggested_action=SuggestedAction.MERGE,
        score=0.8,
    )
    f2 = Flag(
        id="f2",
        source=FlagSource.SIMILARITY,
        severity=Severity.HIGH,
        title="similar",
        evidence="e",
        unit_ids=[u1.id, u2.id],
        suggested_action=SuggestedAction.EXTRACT_SHARED,
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
    from coderazor.config import CoderazorConfig
    from coderazor.stages.analyze import run_analyze

    config = CoderazorConfig(
        workspace=fixtures,
        out_dir=out,
        skip_llm=True,
        analyzers="builtin",
        describe_mode="embed_only",
    )
    artifacts = run_analyze(config)
    assert artifacts.findings is not None
    queue = out / "changesets" / "queue.json"
    assert queue.exists()
    assert (out / "report" / "report.md").exists()
