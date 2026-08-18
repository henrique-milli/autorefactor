from __future__ import annotations

from collections import defaultdict

from autorefactor.models import CodeSpan, CodeUnit, UnitKind, content_hash, unit_id


def simple_name(qualified: str) -> str:
    return qualified.rsplit(".", 1)[-1]


BORING_NAMES = frozenset(
    {
        "equals",
        "hashCode",
        "toString",
        "clone",
        "wait",
        "notify",
        "notifyAll",
        "getClass",
        "finalize",
        "__str__",
        "__repr__",
        "__hash__",
        "__eq__",
    }
)


def is_trivial_unit(unit: CodeUnit, *, min_source_chars: int = 40) -> bool:
    if unit.kind != UnitKind.FUNCTION:
        return False
    name = simple_name(unit.qualified_name)
    if name in BORING_NAMES:
        return True
    if name.startswith("get") and len(unit.source_snippet.strip()) < 120:
        return True
    if len(unit.source_snippet.strip()) < min_source_chars:
        return True
    return False


def filter_units(units: list[CodeUnit]) -> list[CodeUnit]:
    kept = [u for u in units if not is_trivial_unit(u)]
    # Drop orphaned parents? Keep hierarchy even if some children filtered.
    return kept


def ensure_repo_units(units: list[CodeUnit]) -> list[CodeUnit]:
    """Ensure each repo has a synthetic repo-level unit."""
    by_repo: dict[str, list[CodeUnit]] = defaultdict(list)
    for unit in units:
        by_repo[unit.repo].append(unit)

    existing_repos = {u.repo for u in units if u.kind == UnitKind.REPO}
    extras: list[CodeUnit] = []
    for repo, members in by_repo.items():
        if repo in existing_repos:
            continue
        snippet = f"Repository {repo} with {len(members)} indexed units."
        repo_unit = CodeUnit(
            id=unit_id(repo, UnitKind.REPO.value, repo),
            repo=repo,
            path=".",
            qualified_name=repo,
            kind=UnitKind.REPO,
            language="multi",
            source_snippet=snippet,
            content_hash=content_hash(snippet),
            span=CodeSpan(),
        )
        for member in members:
            if member.kind == UnitKind.MODULE and member.parent_id is None:
                member.parent_id = repo_unit.id
        extras.append(repo_unit)
    return extras + units


def children_by_parent(units: list[CodeUnit]) -> dict[str, list[CodeUnit]]:
    mapping: dict[str, list[CodeUnit]] = defaultdict(list)
    for unit in units:
        if unit.parent_id:
            mapping[unit.parent_id].append(unit)
    return mapping


def rollup_description(unit: CodeUnit, children: list[CodeUnit]) -> str:
    parts = [c.description for c in children if c.description]
    if not parts:
        return unit.description or f"{unit.kind.value} {unit.qualified_name}"
    joined = "; ".join(parts[:12])
    return f"{unit.kind.value} {unit.qualified_name}: {joined}"[:800]
