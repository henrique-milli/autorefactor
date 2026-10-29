from __future__ import annotations

import json
from pathlib import Path

from autorefactor.config import AutorefactorConfig
from autorefactor.domain import CodeSpan, CodeUnit, UnitKind, content_hash
from autorefactor.extract.typescript import TypeScriptExtractor
from autorefactor.models import TestGap
from autorefactor.sensors.base import AnalysisContext
from autorefactor.sensors.complexity import measure_complexity
from autorefactor.sensors.slop import _identity_helpers_in_source
from autorefactor.sensors.structure import _dead_api_findings_ts
from autorefactor.sensors.test_quality import _is_shallow_stub
from autorefactor.sensors.wiring import (
    WiringSensor,
    _entry_target_resolvable,
    _package_json_targets,
)
from autorefactor.testing.gaps import suggested_test_path
from autorefactor.testing.stubs import write_ts_characterization_stub

BUTTON_TSX = """\
export function Button({ label }: { label: string }) {
  return <button className="btn">{label}</button>;
}
"""

USE_COUNTER_TS = """\
export function useCounter(start: number) {
  let n = start;
  n = n + 1;
  return n;
}
"""

UTIL_TS = """\
export const trim = (s: string): string => s;
"""


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_tsx_units_labeled_and_react_roles_tagged(tmp_path: Path) -> None:
    repo = tmp_path / "app"
    _write(repo, "src/Button.tsx", BUTTON_TSX)
    _write(repo, "src/useCounter.ts", USE_COUNTER_TS)
    _write(repo, "src/util.ts", UTIL_TS)

    units = TypeScriptExtractor().extract("app", repo)

    button = next(
        u for u in units if u.kind == UnitKind.FUNCTION and u.qualified_name.endswith(".Button")
    )
    assert button.language == "tsx"
    assert button.role == "react_component"

    hook = next(
        u
        for u in units
        if u.kind == UnitKind.FUNCTION and u.qualified_name.endswith(".useCounter")
    )
    assert hook.language == "typescript"
    assert hook.role == "react_hook"

    trim = next(
        u for u in units if u.kind == UnitKind.FUNCTION and u.qualified_name.endswith(".trim")
    )
    assert trim.role == ""
    assert trim.language == "typescript"

    file_unit = next(u for u in units if u.kind == UnitKind.FILE and u.path.endswith(".tsx"))
    assert file_unit.language == "tsx"


def test_complexity_uses_tsx_parser_for_jsx() -> None:
    src = (
        "function View({ x }: { x: boolean }) {\n"
        "  if (x) {\n"
        "    return <div className={x ? 'a' : 'b'}>{x && 'yes'}</div>;\n"
        "  }\n"
        "  return null;\n"
        "}\n"
    )
    nesting, complexity = measure_complexity(src, "tsx")
    assert complexity >= 2
    assert nesting >= 1


def test_identity_helpers_ts_branch() -> None:
    arrow = _identity_helpers_in_source(
        "export const trim = (s: string): string => s;", "typescript"
    )
    assert ("trim", "s") in arrow

    func = _identity_helpers_in_source(
        "function passthrough(x: unknown) {\n  return x;\n}", "tsx"
    )
    assert ("passthrough", "x") in func

    assert (
        _identity_helpers_in_source("export const twice = (x: number) => x * 2;", "typescript")
        == []
    )


def test_dead_api_ts_flags_test_only_symbol(tmp_path: Path) -> None:
    repo = tmp_path / "app"
    _write(repo, "src/lib.ts", "export function usedEverywhere() { return 1; }\n")
    _write(
        repo,
        "src/app.ts",
        'import { usedEverywhere } from "./lib";\n'
        "export const run = () => usedEverywhere();\n",
    )
    _write(
        repo,
        "src/lib.test.ts",
        'import { orphan, usedEverywhere } from "./lib";\n'
        'test("x", () => {\n  orphan();\n  usedEverywhere();\n});\n',
    )

    def fn(path: str, name: str) -> CodeUnit:
        body = f"function {name}() {{ return 1; }}"
        return CodeUnit(
            id=content_hash(f"{path}:{name}"),
            repo="app",
            path=path,
            qualified_name=name,
            kind=UnitKind.FUNCTION,
            language="typescript",
            source_snippet=body,
            content_hash=content_hash(body),
            span=CodeSpan(start_line=1, end_line=1),
        )

    units = [
        fn("src/lib.ts", "usedEverywhere"),
        fn("src/lib.ts", "orphan"),
        fn("src/app.ts", "run"),
    ]
    findings = _dead_api_findings_ts(units, repo, "app", known={u.id for u in units})
    assert len(findings) == 1
    assert findings[0].metadata["symbol"] == "orphan"


def test_suggested_test_path_react_component() -> None:
    component = CodeUnit(
        id=content_hash("btn"),
        repo="app",
        path="src/Button.tsx",
        qualified_name="app.src.Button",
        kind=UnitKind.FUNCTION,
        language="tsx",
        source_snippet=BUTTON_TSX,
        content_hash=content_hash(BUTTON_TSX),
        role="react_component",
    )
    assert suggested_test_path(component).endswith("src/Button.test.tsx")

    plain = CodeUnit(
        id=content_hash("util"),
        repo="app",
        path="src/util.ts",
        qualified_name="app.src.util",
        kind=UnitKind.FUNCTION,
        language="typescript",
        source_snippet=UTIL_TS,
        content_hash=content_hash(UTIL_TS),
    )
    assert suggested_test_path(plain).endswith("src/util.test.ts")


def test_package_json_targets_and_resolvable(tmp_path: Path) -> None:
    repo = tmp_path / "pkg"
    _write(
        repo,
        "package.json",
        json.dumps(
            {
                "main": "./dist/index.js",
                "module": "./src/index.mjs",
                "scripts": {"start": "node scripts/server.ts", "test": "vitest"},
            }
        ),
    )
    _write(repo, "src/index.ts", "export {};\n")
    _write(repo, "scripts/server.ts", "export {};\n")

    targets = dict(_package_json_targets(repo))
    assert targets["main"] == "./dist/index.js"
    assert targets["module"] == "./src/index.mjs"
    assert targets["scripts.start"] == "scripts/server.ts"
    assert "scripts.test" not in targets  # bare command, not a file target

    assert _entry_target_resolvable(repo, "./dist/index.js")  # build output
    assert _entry_target_resolvable(repo, "./src/index.mjs")  # source sibling index.ts
    assert _entry_target_resolvable(repo, "scripts/server.ts")  # exists
    assert not _entry_target_resolvable(repo, "./src/gone.ts")


def test_wiring_finds_dead_package_entry(tmp_path: Path) -> None:
    repo = tmp_path / "app"
    _write(
        repo,
        "package.json",
        json.dumps({"main": "./dist/index.js", "scripts": {"start": "node gone.ts"}}),
    )
    repo_unit = CodeUnit(
        id=content_hash("app"),
        repo="app",
        path="",
        qualified_name="app",
        kind=UnitKind.REPO,
        language="typescript",
        source_snippet="repo",
        content_hash=content_hash("repo"),
    )
    ctx = AnalysisContext(
        workspace=tmp_path,
        repos=(repo,),
        units=(repo_unit,),
        config=AutorefactorConfig(workspace=tmp_path, out_dir=tmp_path / "out"),
        focus="balanced",
    )
    assert WiringSensor().available(ctx)
    findings = WiringSensor().collect(ctx)
    dead = [f for f in findings if f.metadata.get("kind") == "dead_package_entry"]
    assert len(dead) == 1
    assert "gone.ts" in dead[0].evidence


def _gap(language: str, suggested: str, repo: str = "app") -> TestGap:
    return TestGap(
        unit_id="u1",
        repo=repo,
        path="src/lib/calc.ts",
        qualified_name="calc",
        language=language,
        reason="no tests",
        suggested_test_path=suggested,
        finding_ids=["f1"],
    )


def _calc_unit() -> CodeUnit:
    body = "export const calc = (x: number) => x + 1;"
    return CodeUnit(
        id="u1",
        repo="app",
        path="src/lib/calc.ts",
        qualified_name="lib.calc",
        kind=UnitKind.FUNCTION,
        language="typescript",
        source_snippet=body,
        content_hash=content_hash(body),
    )


def test_write_ts_stub_node_runner(tmp_path: Path) -> None:
    _write(tmp_path / "app", "package.json", json.dumps({"name": "app"}))
    created = write_ts_characterization_stub(
        tmp_path, _gap("typescript", "app/src/lib/calc.test.ts"), _calc_unit()
    )
    assert created == "app/src/lib/calc.test.ts"
    text = (tmp_path / "app" / "src" / "lib" / "calc.test.ts").read_text(encoding="utf-8")
    assert 'from "node:test"' in text
    assert "symbol_exists calc" in text
    assert "(autorefactor ensure-tests)" in text
    assert 'const spec = "./calc"' in text


def test_write_ts_stub_vitest_runner(tmp_path: Path) -> None:
    _write(
        tmp_path / "app",
        "package.json",
        json.dumps({"name": "app", "devDependencies": {"vitest": "^3.0.0"}}),
    )
    created = write_ts_characterization_stub(
        tmp_path, _gap("typescript", "app/src/lib/calc.test.ts"), _calc_unit()
    )
    assert created is not None
    text = (tmp_path / "app" / "src" / "lib" / "calc.test.ts").read_text(encoding="utf-8")
    assert 'from "vitest"' in text
    assert "@vite-ignore" in text


def test_write_ts_stub_rejects_other_languages(tmp_path: Path) -> None:
    assert (
        write_ts_characterization_stub(
            tmp_path, _gap("python", "app/tests/test_calc.py"), _calc_unit()
        )
        is None
    )


def test_test_quality_shallow_detection_covers_ts_stubs() -> None:
    shallow = (
        "// Characterization tests for `calc` (autorefactor ensure-tests).\n"
        'import { test } from "node:test";\n'
        'test("symbol_exists calc", async () => {});\n'
    )
    assert _is_shallow_stub(shallow)

    behavioral = shallow + "\nexpect(calc(1)).toEqual(2);\n"
    assert not _is_shallow_stub(behavioral)
