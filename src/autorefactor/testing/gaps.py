from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from autorefactor.domain import CoverageSnapshot
from autorefactor.models import CodeUnit, ConfirmedFinding, TestGap, UnitKind
from autorefactor.testing.inventory import looks_like_test_path

_COVERAGE_PADDING = re.compile(r"CoverageTest|coverage[-_]?padding", re.IGNORECASE)
_PRODUCTION_PATH_HINTS = ("service", "controller", "impl", "handler")
_STRONG_PRODUCTION_HINTS = ("service", "controller", "impl", "handler")
_EXCEPTION_PATH = re.compile(r"Exception\.(?:java|kt)$", re.IGNORECASE)
_EXCEPTION_QNAME = re.compile(r"\w*(?:Exception|Error)$")
_DTO_PATH = re.compile(r"/(?:dto|model|models|exception|exceptions)/", re.IGNORECASE)
_APPLICATION_PATH = re.compile(r"Application\.(?:java|kt)$", re.IGNORECASE)
_WEBMVCTEST = re.compile(r"@WebMvcTest\s*\(\s*(?:value\s*=\s*)?(\w+)(?:\.class)?", re.MULTILINE)
_MOCK_INJECT = re.compile(
    r"@(?:InjectMocks|Mock|Spy)\s*(?:\n\s*)?(?:private|protected|public)?\s+\w+\s+(\w+)",
    re.MULTILINE,
)
_TEST_CLASS_PRODUCTION = re.compile(r"class\s+(\w+?)(?:Test|IT|Tests)\b")
_MOCK_ONLY_EVIDENCE = re.compile(r"webmvctest|mockito|mock-only|mock only", re.IGNORECASE)
_DEFAULT_PRODUCTION_LIMIT = 3
_MOCK_ONLY_PRODUCTION_LIMIT = 8


def is_coverage_padding_path(path: str) -> bool:
    """True for Jacoco padding suites like BonusModelCoverageTest.java."""
    norm = path.replace("\\", "/")
    stem = Path(norm).stem
    return bool(_COVERAGE_PADDING.search(stem) or _COVERAGE_PADDING.search(norm))


def is_test_target_unit(unit: CodeUnit) -> bool:
    """Reject test-path / padding units as ensure_tests primary targets."""
    return looks_like_test_path(unit.path) or is_coverage_padding_path(unit.path)


def _production_class_stem_from_padding(path: str) -> str | None:
    stem = Path(path.replace("\\", "/")).stem
    for suffix in ("CoverageTest", "Test"):
        if stem.endswith(suffix) and len(stem) > len(suffix):
            return stem[: -len(suffix)]
    return None


def _unit_coverage_percent(unit: CodeUnit, coverage: CoverageSnapshot) -> float | None:
    from autorefactor.coverage_paths import coverage_path_matches

    path = unit.path.replace("\\", "/")
    for st in coverage.files or []:
        sp = (st.path or "").replace("\\", "/")
        if coverage_path_matches(path, sp, repo=unit.repo):
            return float(st.percent)
    return None


def _is_trivial_production_unit(unit: CodeUnit) -> bool:
    """Drop 1-LOC exception/DTO/bootstrap types from ensure_tests resolution."""
    path = unit.path.replace("\\", "/")
    base = Path(path).name
    simple = unit.qualified_name.rsplit(".", 1)[-1]
    lower = path.lower()
    if _APPLICATION_PATH.search(base):
        return True
    if _EXCEPTION_PATH.search(base) or _EXCEPTION_QNAME.match(simple):
        return True
    if _DTO_PATH.search(lower):
        return True
    if simple.endswith(("Data", "DTO", "Dto")):
        return True
    return False


def _is_repo_placeholder(unit: CodeUnit) -> bool:
    path = (unit.path or "").replace("\\", "/").strip()
    return unit.kind == UnitKind.REPO or path in {"", ".", unit.repo}


def _production_priority(unit: CodeUnit, coverage: CoverageSnapshot | None) -> float:
    if _is_trivial_production_unit(unit):
        return -100.0
    path = unit.path.replace("\\", "/").lower()
    score = 0.0
    if "src/main/" in path:
        score += 10.0
    if any(h in path for h in _STRONG_PRODUCTION_HINTS):
        score += 8.0
    elif "client" in path and "exception" not in path.lower():
        score += 3.0
    if unit.kind == UnitKind.TYPE:
        score += 2.0
    if unit.kind == UnitKind.FUNCTION:
        score += 1.0
    if coverage and coverage.files:
        pct = _unit_coverage_percent(unit, coverage)
        if pct is not None:
            score += max(0.0, (80.0 - pct) / 10.0)
    return score


def _is_production_unit(unit: CodeUnit) -> bool:
    if is_test_target_unit(unit):
        return False
    path = unit.path.replace("\\", "/").lower()
    if "src/test/" in path:
        return False
    return unit.kind in {
        UnitKind.FUNCTION,
        UnitKind.TYPE,
        UnitKind.PROTO_MESSAGE,
        UnitKind.PROTO_RPC,
        UnitKind.PROTO_SERVICE,
    }


def _pick_production_units(
    repo: str,
    all_units: list[CodeUnit],
    *,
    coverage: CoverageSnapshot | None,
    padding_path: str | None = None,
    limit: int = 3,
) -> list[CodeUnit]:
    """Highest-priority uncovered production units for a repo (or padding sibling)."""
    candidates = [
        u
        for u in all_units
        if u.repo == repo and _is_production_unit(u) and not _is_trivial_production_unit(u)
    ]
    if padding_path:
        stem = _production_class_stem_from_padding(padding_path)
        if stem:
            related = [
                u
                for u in candidates
                if stem.lower() in Path(u.path.replace("\\", "/")).stem.lower()
                or stem.lower() in u.qualified_name.rsplit(".", 1)[-1].lower()
            ]
            if related:
                candidates = related
    ranked = sorted(
        candidates,
        key=lambda u: (-_production_priority(u, coverage), u.qualified_name),
    )
    return ranked[:limit]


def _production_stems_from_mock_tests(repo: str, all_units: list[CodeUnit]) -> set[str]:
    """Infer production class names from WebMvcTest/Mockito suites in src/test."""
    stems: set[str] = set()
    for unit in all_units:
        if unit.repo != repo or not is_test_target_unit(unit):
            continue
        snippet = unit.source_snippet or ""
        path_stem = Path(unit.path.replace("\\", "/")).stem
        for suffix in ("Tests", "Test", "IT"):
            if path_stem.endswith(suffix) and len(path_stem) > len(suffix):
                prod = path_stem[: -len(suffix)]
                if prod and prod[0].isupper():
                    stems.add(prod)
        for match in _WEBMVCTEST.finditer(snippet):
            stems.add(match.group(1))
        for match in _MOCK_INJECT.finditer(snippet):
            stems.add(match.group(1))
        for match in _TEST_CLASS_PRODUCTION.finditer(snippet):
            stems.add(match.group(1))
    return stems


def _units_for_mock_test_stems(
    repo: str,
    stems: set[str],
    all_units: list[CodeUnit],
    *,
    coverage: CoverageSnapshot | None,
    limit: int,
) -> list[CodeUnit]:
    if not stems:
        return []
    matches = [
        u
        for u in all_units
        if u.repo == repo and _is_production_unit(u) and not _is_trivial_production_unit(u)
        and u.qualified_name.rsplit(".", 1)[-1] in stems
    ]
    ranked = sorted(
        matches,
        key=lambda u: (-_production_priority(u, coverage), u.qualified_name),
    )
    return ranked[:limit]


def resolve_production_units_for_ensure_tests(
    target_units: list[CodeUnit],
    all_units: list[CodeUnit],
    *,
    coverage: CoverageSnapshot | None = None,
    evidence_text: str = "",
    prep_kind: str = "",
    limit: int | None = None,
) -> list[CodeUnit]:
    """Map test/padding targets to concrete production FUNCTION/TYPE units."""
    resolved: list[CodeUnit] = []
    seen: set[str] = set()
    evidence_lower = (evidence_text or "").lower()
    mock_only = (
        prep_kind == "java_mock_only"
        or bool(_MOCK_ONLY_EVIDENCE.search(evidence_text or ""))
        or any(_is_repo_placeholder(u) for u in target_units)
    )
    pick_limit = limit if limit is not None else (
        _MOCK_ONLY_PRODUCTION_LIMIT if mock_only else _DEFAULT_PRODUCTION_LIMIT
    )

    def _add(unit: CodeUnit) -> None:
        if unit.id in seen or not _is_production_unit(unit) or _is_trivial_production_unit(unit):
            return
        seen.add(unit.id)
        resolved.append(unit)

    if mock_only:
        for repo in {u.repo for u in target_units}:
            stems = _production_stems_from_mock_tests(repo, all_units)
            for prod in _units_for_mock_test_stems(
                repo, stems, all_units, coverage=coverage, limit=pick_limit
            ):
                _add(prod)

    for unit in target_units:
        if _is_production_unit(unit):
            _add(unit)
            continue
        if is_test_target_unit(unit):
            for prod in _pick_production_units(
                unit.repo,
                all_units,
                coverage=coverage,
                padding_path=unit.path,
                limit=pick_limit,
            ):
                _add(prod)

    if evidence_lower:
        for u in all_units:
            if not _is_production_unit(u) or _is_trivial_production_unit(u):
                continue
            qn = u.qualified_name.lower()
            simple = u.qualified_name.rsplit(".", 1)[-1].lower()
            if qn in evidence_lower or (
                len(simple) > 8 and simple in evidence_lower
            ):
                _add(u)

    if not resolved:
        repos = {u.repo for u in target_units}
        for repo in repos:
            for prod in _pick_production_units(
                repo, all_units, coverage=coverage, limit=pick_limit
            ):
                _add(prod)

    return resolved[:pick_limit]


def resolve_mapper_caller_units(
    mapper_unit: CodeUnit,
    all_units: list[CodeUnit],
    *,
    max_callers: int = 3,
) -> list[CodeUnit]:
    """Find src/main service methods that invoke a draft-map mapper TYPE."""
    mapper_name = mapper_unit.qualified_name.rsplit(".", 1)[-1]
    mapper_methods = [
        u.qualified_name.rsplit(".", 1)[-1]
        for u in all_units
        if u.parent_id == mapper_unit.id and u.kind == UnitKind.FUNCTION
    ]
    if not mapper_methods:
        mapper_methods = ["mapToProductId", "map", "convert", "toProductId"]

    callers: list[tuple[float, CodeUnit]] = []
    for u in all_units:
        if u.repo != mapper_unit.repo or u.kind != UnitKind.FUNCTION:
            continue
        if is_test_target_unit(u):
            continue
        path = u.path.replace("\\", "/").lower()
        if "src/main/" not in path and "/service/" not in path:
            continue
        snippet = u.source_snippet or ""
        if mapper_name not in snippet and not any(m in snippet for m in mapper_methods):
            continue
        callers.append((_production_priority(u, None), u))
    callers.sort(key=lambda t: (-t[0], t[1].qualified_name))
    return [u for _, u in callers[:max_callers]]


def _class_name(unit: CodeUnit) -> str | None:
    """Return enclosing TYPE name for methods, or the type's own simple name."""
    if unit.kind == UnitKind.TYPE:
        name = unit.qualified_name.rsplit(".", 1)[-1]
        return name if name and name[0].isupper() else None
    if unit.kind == UnitKind.FUNCTION and "." in unit.qualified_name:
        parent = unit.qualified_name.rsplit(".", 1)[0].rsplit(".", 1)[-1]
        if parent and parent[0].isupper():
            return parent
    return None


def suggested_test_path(unit: CodeUnit) -> str:
    path = unit.path.replace("\\", "/")
    name = Path(path).stem
    simple = unit.qualified_name.rsplit(".", 1)[-1]
    class_sym = _class_name(unit)
    test_symbol = class_sym or simple

    if unit.language == "python":
        return f"{unit.repo}/tests/test_{unit.repo}_{name}_{test_symbol}.py"
    if unit.language == "java":
        javaish = path
        if "src/main/java/" in javaish:
            javaish = javaish.replace("src/main/java/", "src/test/java/")
            stem = class_sym or name
            return f"{unit.repo}/{javaish.removesuffix('.java').rsplit('/', 1)[0]}/{stem}Test.java"
        stem = class_sym or name
        return f"{unit.repo}/src/test/java/{stem}Test.java"
    if unit.language in {"typescript", "javascript"}:
        parent = str(Path(path).parent).replace("\\", "/")
        if parent in {"", "."}:
            parent = "tests"
        stem = class_sym or name
        return f"{unit.repo}/{parent}/{stem}.test.ts"
    if unit.language == "go":
        stem = class_sym or name
        return f"{unit.repo}/{Path(path).with_name(stem + '_test.go')}"
    return f"{unit.repo}/tests/test_{unit.repo}_{name}.md"


def _dedupe_key(unit: CodeUnit) -> str:
    """One gap per suggested test file (class + methods collapse together)."""
    return suggested_test_path(unit).lower()


def _type_parent_id(unit: CodeUnit, by_id: dict[str, CodeUnit]) -> str | None:
    """Resolve TYPE unit id for a method, walking parent_id when present."""
    if unit.kind == UnitKind.TYPE:
        return unit.id
    if unit.parent_id and unit.parent_id in by_id:
        parent = by_id[unit.parent_id]
        if parent.kind == UnitKind.TYPE:
            return parent.id
    class_sym = _class_name(unit)
    if not class_sym:
        return None
    for u in by_id.values():
        if (
            u.repo == unit.repo
            and u.kind == UnitKind.TYPE
            and u.qualified_name.rsplit(".", 1)[-1] == class_sym
        ):
            return u.id
    return None


def unit_has_characterization_test(unit: CodeUnit, existing_tests: list[str]) -> bool:
    """True when a nearby test file already covers the unit (class/symbol stem match)."""
    existing_lower = {p.lower() for p in existing_tests}
    suggested = suggested_test_path(unit)
    if suggested.lower() in existing_lower:
        return True
    stem = Path(unit.path).stem.lower()
    class_sym = _class_name(unit)
    return any(
        (
            stem in Path(t).stem.lower()
            or (class_sym and class_sym.lower() in Path(t).stem.lower())
        )
        and unit.repo == t.split("/", 1)[0]
        for t in existing_tests
    )


def find_test_gaps(
    findings: list[ConfirmedFinding],
    units: list[CodeUnit],
    existing_tests: list[str],
    *,
    coverage: CoverageSnapshot | None = None,
) -> list[TestGap]:
    by_id = {u.id: u for u in units}
    existing_lower = {p.lower() for p in existing_tests}
    tests_by_repo: dict[str, list[str]] = defaultdict(list)
    for path in existing_tests:
        repo = path.split("/", 1)[0]
        tests_by_repo[repo].append(path)

    gaps: dict[str, TestGap] = {}
    for finding in findings:
        if not finding.valid:
            continue
        for uid in finding.flag.unit_ids:
            unit = by_id.get(uid)
            if not unit:
                continue
            if unit.kind not in {
                UnitKind.FUNCTION,
                UnitKind.TYPE,
                UnitKind.PROTO_MESSAGE,
                UnitKind.PROTO_RPC,
                UnitKind.PROTO_SERVICE,
            }:
                continue
            if is_test_target_unit(unit):
                prod = resolve_production_units_for_ensure_tests(
                    [unit],
                    list(by_id.values()),
                    coverage=coverage,
                    evidence_text=finding.finding.evidence or "",
                )
                if not prod:
                    continue
                unit = prod[0]
            if Path(unit.path).name in {"__init__.py", "index.ts", "index.js"}:
                continue
            simple = unit.qualified_name.rsplit(".", 1)[-1]
            if simple in {"__init__", "<module>"}:
                continue

            suggested = suggested_test_path(unit)
            covered = suggested.lower() in existing_lower
            if not covered:
                covered = unit_has_characterization_test(unit, existing_tests)
            if covered:
                continue

            reason = "No nearby characterization tests found for a confirmed refactor target."
            if not tests_by_repo.get(unit.repo):
                reason = (
                    "Repo has no detected tests; characterization suite required before refactor."
                )

            key = _dedupe_key(unit)
            existing_gap = gaps.get(key)
            type_id = _type_parent_id(unit, by_id)
            if existing_gap is None:
                rep_id = type_id or unit.id
                rep = by_id.get(rep_id, unit)
                gaps[key] = TestGap(
                    unit_id=rep_id,
                    repo=unit.repo,
                    path=rep.path,
                    qualified_name=rep.qualified_name,
                    language=unit.language,
                    reason=reason,
                    suggested_test_path=suggested,
                    finding_ids=[finding.flag.id],
                )
            else:
                if finding.flag.id not in existing_gap.finding_ids:
                    existing_gap.finding_ids.append(finding.flag.id)
                if type_id:
                    rep = by_id[type_id]
                    existing_gap.unit_id = type_id
                    existing_gap.qualified_name = rep.qualified_name
                    existing_gap.path = rep.path

    return sorted(gaps.values(), key=lambda g: (g.repo, g.path, g.qualified_name))


# Back-compat alias
_suggested_test_path = suggested_test_path
