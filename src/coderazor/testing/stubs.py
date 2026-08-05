from __future__ import annotations

import re
import textwrap
from pathlib import Path

from coderazor.models import CodeUnit, TestGap, TestPlan


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


def write_python_characterization_stub(workspace: Path, gap: TestGap, unit: CodeUnit) -> str | None:
    """Write a minimal pytest characterization stub for Python gaps (skip_llm path)."""
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
    target = workspace / gap.repo / rel_in_repo
    target.parent.mkdir(parents=True, exist_ok=True)

    simple = unit.qualified_name.rsplit(".", 1)[-1]
    name = Path(unit.path).stem
    # Prefer asserting the defining module symbol for functions/classes
    symbol = simple
    if unit.kind.value == "function" and "." in unit.qualified_name:
        # class methods: assert the class exists
        parent_sym = unit.qualified_name.rsplit(".", 2)[-2]
        if parent_sym and parent_sym[0].isupper():
            symbol = parent_sym

    ident = _safe_ident(f"{gap.repo}_{name}_{symbol}")
    body = textwrap.dedent(
        f'''\
        """Characterization tests for `{unit.qualified_name}` (coderazor ensure-tests)."""

        from __future__ import annotations

        import importlib
        import sys
        from pathlib import Path

        # Repo root (parent of tests/)
        _REPO_ROOT = Path(__file__).resolve().parents[1]
        if str(_REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(_REPO_ROOT))

        mod = importlib.import_module("{mod}")


        def test_{ident}_is_importable():
            assert mod is not None


        def test_{ident}_symbol_exists():
            assert hasattr(mod, "{symbol}")
        '''
    )
    if not target.exists():
        target.write_text(body, encoding="utf-8")
    return f"{gap.repo}/{rel_in_repo}"


def write_skip_llm_test_stubs(
    workspace: Path,
    test_plan: TestPlan,
    units: list[CodeUnit],
) -> list[str]:
    by_id = {u.id: u for u in units}
    created: list[str] = []
    for gap in test_plan.gaps:
        unit = by_id.get(gap.unit_id)
        if not unit:
            continue
        path = write_python_characterization_stub(workspace, gap, unit)
        if path and path not in created:
            created.append(path)
    return created
