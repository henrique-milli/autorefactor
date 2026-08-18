from __future__ import annotations

from pathlib import Path

from autorefactor.domain import CodeSpan, CodeUnit, TestGap, UnitKind, content_hash, unit_id
from autorefactor.testing.stubs import write_python_characterization_stub, write_skip_llm_test_stubs
from autorefactor.models import TestPlan


def _gap(uid: str, qname: str, path: str = "alpha/util.py") -> tuple[TestGap, CodeUnit]:
    unit = CodeUnit(
        id=uid,
        repo="alpha",
        path=path,
        qualified_name=qname,
        kind=UnitKind.FUNCTION,
        language="python",
        source_snippet="pass",
        content_hash=content_hash(uid),
        span=CodeSpan(start_line=1, end_line=3),
    )
    gap = TestGap(
        unit_id=uid,
        repo="alpha",
        path=path,
        qualified_name=qname,
        language="python",
        reason="test",
        suggested_test_path=f"alpha/tests/test_alpha_util_{qname.rsplit('.', 1)[-1]}.py",
    )
    return gap, unit


def test_stub_writes_conftest_and_parametrize(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    repo = ws / "alpha"
    (repo / "alpha").mkdir(parents=True)
    (repo / "alpha" / "util.py").write_text(
        "def normalize_user_id(raw):\n    return raw\n", encoding="utf-8"
    )
    gap, unit = _gap(unit_id("n"), "normalize_user_id")
    gap = gap.model_copy(update={"suggested_test_path": "alpha/tests/test_alpha_util_normalize_user_id.py"})
    path = write_python_characterization_stub(ws, gap, unit)
    assert path
    conftest = repo / "tests" / "conftest.py"
    assert conftest.is_file()
    body = (repo / "tests" / "test_alpha_util_normalize_user_id.py").read_text(encoding="utf-8")
    assert "pytest.mark.parametrize" in body
    assert "_REPO_ROOT" not in body


def test_score_aggregator_single_stub(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    repo = ws / "alpha"
    (repo / "alpha").mkdir(parents=True)
    (repo / "alpha" / "util.py").write_text("class ScoreAggregator:\n    pass\n", encoding="utf-8")
    type_u = CodeUnit(
        id=unit_id("t"),
        repo="alpha",
        path="alpha/util.py",
        qualified_name="ScoreAggregator",
        kind=UnitKind.TYPE,
        language="python",
        source_snippet="class ScoreAggregator: pass",
        content_hash=content_hash("t"),
        span=CodeSpan(start_line=1, end_line=2),
    )
    add = CodeUnit(
        id=unit_id("a"),
        repo="alpha",
        path="alpha/util.py",
        qualified_name="ScoreAggregator.add",
        kind=UnitKind.FUNCTION,
        language="python",
        source_snippet="def add(self): pass",
        content_hash=content_hash("a"),
        parent_id=type_u.id,
        span=CodeSpan(start_line=3, end_line=4),
    )
    snap = CodeUnit(
        id=unit_id("s"),
        repo="alpha",
        path="alpha/util.py",
        qualified_name="ScoreAggregator.snapshot",
        kind=UnitKind.FUNCTION,
        language="python",
        source_snippet="def snapshot(self): pass",
        content_hash=content_hash("s"),
        parent_id=type_u.id,
        span=CodeSpan(start_line=5, end_line=6),
    )
    gap = TestGap(
        unit_id=type_u.id,
        repo="alpha",
        path="alpha/util.py",
        qualified_name="ScoreAggregator",
        language="python",
        reason="test",
        suggested_test_path="alpha/tests/test_alpha_util_ScoreAggregator.py",
    )
    write_python_characterization_stub(ws, gap, type_u, sibling_units=[type_u, add, snap])
    test_file = repo / "tests" / "test_alpha_util_ScoreAggregator.py"
    assert test_file.is_file()
    body = test_file.read_text(encoding="utf-8")
    assert "add_and_snapshot" in body
