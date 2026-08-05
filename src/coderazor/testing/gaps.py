from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from coderazor.models import CodeUnit, ConfirmedFinding, TestGap, UnitKind
from coderazor.testing.inventory import looks_like_test_path


def suggested_test_path(unit: CodeUnit) -> str:
    path = unit.path.replace("\\", "/")
    name = Path(path).stem
    simple = unit.qualified_name.rsplit(".", 1)[-1]

    if unit.language == "python":
        return f"{unit.repo}/tests/test_{unit.repo}_{name}_{simple}.py"
    if unit.language == "java":
        javaish = path
        if "src/main/java/" in javaish:
            javaish = javaish.replace("src/main/java/", "src/test/java/")
            return f"{unit.repo}/{javaish.removesuffix('.java')}Test.java"
        return f"{unit.repo}/src/test/java/{name}Test.java"
    if unit.language in {"typescript", "javascript"}:
        parent = str(Path(path).parent).replace("\\", "/")
        if parent in {"", "."}:
            parent = "tests"
        return f"{unit.repo}/{parent}/{name}.test.ts"
    if unit.language == "go":
        return f"{unit.repo}/{Path(path).with_name(name + '_test.go')}"
    return f"{unit.repo}/tests/test_{unit.repo}_{name}.md"


def _dedupe_key(unit: CodeUnit) -> str:
    """One gap per suggested test file (class + methods collapse together)."""
    return suggested_test_path(unit).lower()


def find_test_gaps(
    findings: list[ConfirmedFinding],
    units: list[CodeUnit],
    existing_tests: list[str],
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
            if looks_like_test_path(unit.path):
                continue
            if Path(unit.path).name in {"__init__.py", "index.ts", "index.js"}:
                continue
            simple = unit.qualified_name.rsplit(".", 1)[-1]
            if simple in {"__init__", "<module>"}:
                continue

            suggested = suggested_test_path(unit)
            covered = suggested.lower() in existing_lower
            if not covered:
                stem = Path(unit.path).stem.lower()
                covered = any(
                    stem in Path(t).stem.lower() and unit.repo == t.split("/", 1)[0]
                    for t in existing_tests
                )
            if covered:
                continue

            reason = "No nearby characterization tests found for a confirmed refactor target."
            if not tests_by_repo.get(unit.repo):
                reason = (
                    "Repo has no detected tests; characterization suite required before refactor."
                )

            key = _dedupe_key(unit)
            existing_gap = gaps.get(key)
            if existing_gap is None:
                # Prefer TYPE unit as the representative when collapsing methods
                gaps[key] = TestGap(
                    unit_id=unit.id,
                    repo=unit.repo,
                    path=unit.path,
                    qualified_name=unit.qualified_name,
                    language=unit.language,
                    reason=reason,
                    suggested_test_path=suggested,
                    finding_ids=[finding.flag.id],
                )
            else:
                if finding.flag.id not in existing_gap.finding_ids:
                    existing_gap.finding_ids.append(finding.flag.id)
                if unit.kind == UnitKind.TYPE and by_id.get(existing_gap.unit_id) and by_id[
                    existing_gap.unit_id
                ].kind != UnitKind.TYPE:
                    existing_gap.unit_id = unit.id
                    existing_gap.qualified_name = unit.qualified_name
                    existing_gap.path = unit.path

    return sorted(gaps.values(), key=lambda g: (g.repo, g.path, g.qualified_name))


# Back-compat alias
_suggested_test_path = suggested_test_path
