from __future__ import annotations

from pathlib import Path

from coderazor.models import CodeUnit


def units_covering_line(units: list[CodeUnit], rel_path: str, line: int) -> list[CodeUnit]:
    """Match units whose path ends with rel_path and span covers ``line``."""
    norm = rel_path.replace("\\", "/")
    matches: list[CodeUnit] = []
    for unit in units:
        unit_path = f"{unit.repo}/{unit.path}".replace("\\", "/")
        if unit_path != norm and not unit_path.endswith("/" + norm) and unit.path.replace("\\", "/") != norm:
            # Also accept repo-relative path without workspace prefix
            if not (
                unit.path.replace("\\", "/") == norm
                or norm.endswith("/" + unit.path.replace("\\", "/"))
                or norm.endswith(unit.path.replace("\\", "/"))
            ):
                continue
        start = unit.span.start_line or 0
        end = unit.span.end_line or start
        if line and start and end and not (start <= line <= end):
            continue
        matches.append(unit)
    # Prefer tightest span
    matches.sort(key=lambda u: (u.span.end_line or 10**9) - (u.span.start_line or 0))
    return matches[:5]


def repo_relative(workspace: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(workspace.resolve())).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")
