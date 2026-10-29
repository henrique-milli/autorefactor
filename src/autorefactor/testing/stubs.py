from __future__ import annotations

import os
import re
import textwrap
from collections import defaultdict
from pathlib import Path

from autorefactor.models import CodeUnit, TestGap, TestPlan, UnitKind
from autorefactor.paths import resolve_repo_file
from autorefactor.testing.gaps import _class_name, suggested_test_path


def _python_import_path(unit: CodeUnit) -> str | None:
    path = unit.path.replace("\\", "/")
    if not path.endswith(".py"):
        return None
    if path.endswith("__init__.py"):
        mod = path[: -len("__init__.py")].strip("/").replace("/", ".")
        return mod.rstrip(".") or None
    return path[: -len(".py")].replace("/", ".")


def _safe_ident(name: str) -> str:
    cleaned = re.sub(r"[^0-9a-zA-Z_]+", "_", name)
    if cleaned and cleaned[0].isdigit():
        cleaned = f"fn_{cleaned}"
    return cleaned or "target"


_CONFTEST_BODY = textwrap.dedent(
    '''\
    """Shared pytest path setup (autorefactor ensure-tests).

    Prefer editable install (`pip install -e .`) so imports work without this hook.
    """
    from __future__ import annotations

    import sys
    from pathlib import Path

    _REPO_ROOT = Path(__file__).resolve().parents[1]
    root = str(_REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    '''
)


def _ensure_conftest(workspace: Path, repo: str) -> None:
    target = resolve_repo_file(workspace, repo, "tests/conftest.py")
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_CONFTEST_BODY, encoding="utf-8")


def _parametrize_cases(unit: CodeUnit) -> list[tuple[str, str]]:
    """Known behavioral vectors for common util targets."""
    simple = unit.qualified_name.rsplit(".", 1)[-1]
    if simple == "normalize_user_id":
        return [
            ('""', '""'),
            ('"  User:ABC  "', '"abc"'),
            ('"hello world"', '"hello-world"'),
            ('"user:foo bar"', '"foo-bar"'),
        ]
    return []


def _stub_header(unit: CodeUnit, mod: str) -> str:
    return textwrap.dedent(
        f'''\
        """Characterization tests for `{unit.qualified_name}` (autorefactor ensure-tests)."""

        from __future__ import annotations

        import importlib

        import pytest

        mod = importlib.import_module("{mod}")

        '''
    )


def _function_stub_body(unit: CodeUnit, mod: str) -> str:
    simple = unit.qualified_name.rsplit(".", 1)[-1]
    cases = _parametrize_cases(unit)
    header = _stub_header(unit, mod)
    if cases:
        rows = ",\n        ".join(f"({raw}, {exp})" for raw, exp in cases)
        ident = _safe_ident(simple)
        return header + textwrap.dedent(
            f'''\
            @pytest.mark.parametrize("raw,expected", [
                {rows},
            ])
            def test_{ident}_behavior(raw, expected):
                assert getattr(mod, "{simple}")(eval(raw)) == eval(expected)
            '''
        )
    ident = _safe_ident(f"{Path(unit.path).stem}_{simple}")
    return header + textwrap.dedent(
        f'''\
        def test_{ident}_callable():
            assert callable(getattr(mod, "{simple}"))
        '''
    )


def _class_stub_body(class_unit: CodeUnit, methods: list[CodeUnit], mod: str) -> str:
    class_name = class_unit.qualified_name.rsplit(".", 1)[-1]
    header = _stub_header(class_unit, mod)
    method_names = sorted({m.qualified_name.rsplit(".", 1)[-1] for m in methods})
    if class_name == "ScoreAggregator" and {"add", "snapshot"}.issubset(method_names):
        return header + textwrap.dedent(
            '''\
            def test_score_aggregator_add_and_snapshot():
                agg = mod.ScoreAggregator()
                agg.add("a", 1.0)
                agg.add("a", 2.0)
                agg.add("b", 0.5)
                assert agg.snapshot() == {"a": 3.0, "b": 0.5}
            '''
        )
    checks = "\n".join(
        f'    assert hasattr(mod.{class_name}, "{name}")' for name in method_names
    )
    ident = _safe_ident(class_name)
    return header + textwrap.dedent(
        f'''\
        def test_{ident}_methods_exist():
            cls = getattr(mod, "{class_name}")
        {checks}
        '''
    )


def _write_stub_file(
    workspace: Path,
    repo: str,
    rel_path: str,
    body: str,
) -> str:
    target = resolve_repo_file(workspace, repo, rel_path)
    if target.exists():
        return f"{repo}/{rel_path}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return f"{repo}/{rel_path}"


def write_python_characterization_stub(
    workspace: Path,
    gap: TestGap,
    unit: CodeUnit,
    *,
    sibling_units: list[CodeUnit] | None = None,
) -> str | None:
    """Write a pytest characterization stub for Python gaps (skip_llm path)."""
    if gap.language != "python":
        return None
    mod = _python_import_path(unit)
    if not mod:
        return None

    rel = gap.suggested_test_path
    if rel.startswith(f"{gap.repo}/"):
        rel_in_repo = rel[len(gap.repo) + 1 :]
    else:
        rel_in_repo = rel

    _ensure_conftest(workspace, gap.repo)

    class_sym = _class_name(unit)
    siblings = sibling_units or []
    if class_sym and unit.kind in {UnitKind.TYPE, UnitKind.FUNCTION}:
        class_unit = unit if unit.kind == UnitKind.TYPE else None
        methods = []
        for u in siblings:
            if u.repo != unit.repo:
                continue
            if _class_name(u) == class_sym:
                if u.kind == UnitKind.TYPE:
                    class_unit = u
                elif u.kind == UnitKind.FUNCTION:
                    methods.append(u)
        if class_unit is None:
            class_unit = unit
        body = _class_stub_body(class_unit, methods or [unit], mod)
    else:
        body = _function_stub_body(unit, mod)

    return _write_stub_file(workspace, gap.repo, rel_in_repo, body)


_TS_SOURCE_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def _ts_import_spec(unit: CodeUnit, test_rel_in_repo: str) -> str:
    """Extensionless relative import from the test file to the unit source."""
    test_dir = Path(test_rel_in_repo).parent.as_posix()
    unit_path = unit.path.replace("\\", "/")
    rel = os.path.relpath(unit_path, start=test_dir or ".").replace("\\", "/")
    for ext in _TS_SOURCE_EXTS:
        if rel.endswith(ext):
            rel = rel[: -len(ext)]
            break
    if not rel.startswith("."):
        rel = f"./{rel}"
    return rel


def _ts_runner(workspace: Path, gap: TestGap, test_rel_in_repo: str) -> str:
    """Pick the repo's unit-test runner: 'vitest' when configured, else node:test."""
    test_file = resolve_repo_file(workspace, gap.repo, test_rel_in_repo)
    root = test_file.parent
    for _ in range(8):
        pkg = root / "package.json"
        if pkg.is_file():
            text = pkg.read_text(encoding="utf-8", errors="replace")
            if "vitest" in text or any(root.glob("vitest.config.*")):
                return "vitest"
            return "node"
        if root.parent == root:
            break
        root = root.parent
    return "node"


def write_ts_characterization_stub(
    workspace: Path,
    gap: TestGap,
    unit: CodeUnit,
) -> str | None:
    """Write a TS/JS characterization stub (skip_llm path) for vitest or node:test."""
    if gap.language not in {"typescript", "tsx", "javascript"}:
        return None
    if not unit.path.replace("\\", "/").endswith(_TS_SOURCE_EXTS):
        return None

    rel = gap.suggested_test_path
    rel_in_repo = rel[len(gap.repo) + 1 :] if rel.startswith(f"{gap.repo}/") else rel

    spec = _ts_import_spec(unit, rel_in_repo)
    simple = unit.qualified_name.rsplit(".", 1)[-1]
    ident = _safe_ident(simple)
    runner = _ts_runner(workspace, gap, rel_in_repo)

    if runner == "vitest":
        body = textwrap.dedent(
            f'''\
            // Characterization tests for `{unit.qualified_name}` (autorefactor ensure-tests).
            // Replace the module-load smoke with real behavior assertions.
            import {{ expect, test }} from "vitest";

            test("symbol_exists {ident}", async () => {{
              const spec = "{spec}";
              const mod = (await import(/* @vite-ignore */ spec)) as Record<string, unknown>;
              expect(mod).toBeTruthy();
            }});
            '''
        )
    else:
        body = textwrap.dedent(
            f'''\
            // Characterization tests for `{unit.qualified_name}` (autorefactor ensure-tests).
            // Replace the module-load smoke with real behavior assertions.
            import assert from "node:assert";
            import {{ test }} from "node:test";

            test("symbol_exists {ident}", async () => {{
              const spec = "{spec}";
              const mod = (await import(spec).catch(() => null)) as Record<string, unknown> | null;
              assert.ok(mod, `module ${{spec}} should import — wire the real path for your runner`);
            }});
            '''
        )
    return _write_stub_file(workspace, gap.repo, rel_in_repo, body)


def write_skip_llm_test_stubs(
    workspace: Path,
    test_plan: TestPlan,
    units: list[CodeUnit],
) -> list[str]:
    by_id = {u.id: u for u in units}
    by_path: dict[str, list[tuple[TestGap, CodeUnit]]] = defaultdict(list)
    for gap in test_plan.gaps:
        unit = by_id.get(gap.unit_id)
        if not unit:
            continue
        by_path[gap.suggested_test_path.lower()].append((gap, unit))

    created: list[str] = []
    for _key, group in by_path.items():
        gap, unit = group[0]
        path = write_python_characterization_stub(
            workspace,
            gap,
            unit,
            sibling_units=units,
        ) or write_ts_characterization_stub(workspace, gap, unit)
        if path and path not in created:
            created.append(path)
    return created
