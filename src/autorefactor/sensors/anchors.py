"""Resolve file-scoped units for sensor findings — never REPO / path='.'."""

from __future__ import annotations

from pathlib import Path

from autorefactor.domain import CodeUnit, UnitKind
from autorefactor.index.hierarchy import simple_name
from autorefactor.sensors.base import AnalysisContext
from autorefactor.sensors.similarity import is_test_path


def norm_path(path: str) -> str:
    return (path or "").replace("\\", "/").lstrip("./").strip()


def is_repo_root_path(path: str, repo: str | None = None) -> bool:
    p = norm_path(path)
    return p in {"", ".", "/"} or (bool(repo) and p == repo)


def resolve_file_unit(ctx: AnalysisContext, repo: str, rel: str) -> CodeUnit | None:
    """Prefer a TYPE (then FILE) for ``rel``; never fall back to REPO / path='.'."""
    norm = norm_path(rel)
    if not norm or is_repo_root_path(norm, repo):
        return None
    stem = Path(norm).stem
    type_match: CodeUnit | None = None
    file_match: CodeUnit | None = None
    stem_match: CodeUnit | None = None
    for unit in ctx.units:
        if unit.repo != repo or unit.kind == UnitKind.REPO:
            continue
        up = norm_path(unit.path or "")
        if is_repo_root_path(up, repo):
            continue
        if up == norm or up.endswith("/" + norm) or (norm.endswith(up) and up):
            if unit.kind == UnitKind.TYPE:
                return unit
            if unit.kind == UnitKind.FILE and file_match is None:
                file_match = unit
            elif type_match is None and unit.kind not in {UnitKind.MODULE, UnitKind.REPO}:
                type_match = unit
        if (
            unit.kind == UnitKind.TYPE
            and stem
            and simple_name(unit.qualified_name) == stem
        ):
            stem_match = unit
    return type_match or file_match or stem_match


def production_type_for_repo(ctx: AnalysisContext, repo: str) -> CodeUnit | None:
    """First non-test TYPE in ``src/main`` (else any production TYPE). Never REPO."""
    types: list[CodeUnit] = []
    for unit in ctx.units:
        if unit.repo != repo or unit.kind != UnitKind.TYPE:
            continue
        if is_test_path(unit.path) or is_repo_root_path(unit.path, repo):
            continue
        types.append(unit)
    main = [u for u in types if "src/main/" in norm_path(u.path)]
    pool = main or types
    return pool[0] if pool else None
