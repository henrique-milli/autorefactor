from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree

from autorefactor.coverage_paths import normalize_jacoco_rel_path
from autorefactor.domain import CoverageFileStat, CoverageSnapshot


def parse_coverage_py_json(path: Path) -> CoverageSnapshot:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError("coverage.py JSON root must be object")
    files_obj = data.get("files") or {}
    if not isinstance(files_obj, dict):
        raise TypeError("coverage.py 'files' must be object")
    files: list[CoverageFileStat] = []
    totals = data.get("totals") or {}
    if not isinstance(totals, dict):
        totals = {}
    for file_path, info in files_obj.items():
        if not isinstance(info, dict):
            continue
        summary = info.get("summary") or {}
        if not isinstance(summary, dict):
            summary = {}
        covered = int(summary.get("covered_lines") or 0)
        total = int(summary.get("num_statements") or 0)
        percent = float(summary.get("percent_covered") or 0.0)
        files.append(
            CoverageFileStat(
                path=file_path.replace("\\", "/"),
                covered_lines=covered,
                total_lines=total,
                percent=percent,
            )
        )
    overall = totals.get("percent_covered")
    return CoverageSnapshot(
        summary=f"coverage.py report from {path.name}",
        overall_percent=float(overall) if overall is not None else None,
        files=files,
        source="coverage.py",
    )


def parse_istanbul_json(path: Path) -> CoverageSnapshot:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError("istanbul JSON root must be object")
    files: list[CoverageFileStat] = []
    covered_sum = 0
    total_sum = 0
    for file_path, info in data.items():
        if not isinstance(info, dict):
            continue
        statement_map = info.get("statementMap") or {}
        s = info.get("s") or {}
        if not isinstance(statement_map, dict):
            statement_map = {}
        if not isinstance(s, dict):
            s = {}
        total = len(statement_map) or len(s)
        covered = sum(1 for v in s.values() if int(v) > 0)
        percent = (100.0 * covered / total) if total else 0.0
        covered_sum += covered
        total_sum += total
        files.append(
            CoverageFileStat(
                path=str(file_path).replace("\\", "/"),
                covered_lines=covered,
                total_lines=total,
                percent=percent,
            )
        )
    overall = (100.0 * covered_sum / total_sum) if total_sum else None
    return CoverageSnapshot(
        summary=f"Istanbul/nyc coverage from {path.name}",
        overall_percent=overall,
        files=files,
        source="istanbul",
    )


def _jacoco_local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _jacoco_children(parent, local_name: str):
    return [el for el in parent if _jacoco_local_tag(el.tag) == local_name]


def parse_jacoco_xml(path: Path) -> CoverageSnapshot:
    root = ElementTree.parse(path).getroot()
    files: list[CoverageFileStat] = []
    for package in _jacoco_children(root, "package"):
        pkg = package.attrib.get("name", "").replace(".", "/")
        for source in _jacoco_children(package, "sourcefile"):
            name = source.attrib.get("name") or ""
            rel = f"{pkg}/{name}" if pkg else name
            covered = 0
            total = 0
            for counter in _jacoco_children(source, "counter"):
                if counter.attrib.get("type") == "LINE":
                    covered = int(counter.attrib.get("covered") or 0)
                    missed = int(counter.attrib.get("missed") or 0)
                    total = covered + missed
            percent = (100.0 * covered / total) if total else 0.0
            files.append(
                CoverageFileStat(
                    path=normalize_jacoco_rel_path(rel.replace("\\", "/")),
                    covered_lines=covered,
                    total_lines=total,
                    percent=percent,
                )
            )
    overall = None
    for counter in _jacoco_children(root, "counter"):
        if counter.attrib.get("type") == "LINE":
            covered = int(counter.attrib.get("covered") or 0)
            missed = int(counter.attrib.get("missed") or 0)
            total = covered + missed
            overall = (100.0 * covered / total) if total else 0.0
    return CoverageSnapshot(
        summary=f"JaCoCo report from {path.name}",
        overall_percent=overall,
        files=files,
        source="jacoco",
    )


def _is_maven_target_coverage_json(path: Path) -> bool:
    """True for Maven target/**/coverage.json (often non-coverage JSON lists)."""
    parts = [p.lower() for p in path.parts]
    if "target" not in parts:
        return False
    return path.name.lower() == "coverage.json"


_JACOCO_GLOBS = (
    "target/site/jacoco/jacoco.xml",
    "**/target/site/jacoco/jacoco.xml",
    "build/reports/jacoco/test/jacocoTestReport.xml",
    "**/build/reports/jacoco/test/jacocoTestReport.xml",
    "**/jacoco.xml",
    "**/jacoco-aggregate/jacoco.xml",
)


def _coverage_search_roots(workspace: Path, repos: list[Path]) -> list[Path]:
    """Workspace, each repo, and Maven aggregator parents for nested modules."""
    roots: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        try:
            key = str(path.resolve())
        except OSError:
            return
        if key not in seen:
            seen.add(key)
            roots.append(path)

    add(workspace)
    for repo in repos:
        add(repo)
        # Walk Maven parent chain — JaCoCo often lands at aggregator target/.
        parent = repo.parent
        while parent != repo and (parent / "pom.xml").is_file():
            add(parent)
            if parent == workspace or parent.parent == parent:
                break
            parent = parent.parent
    return roots


def discover_coverage_reports(workspace: Path, repos: list[Path]) -> list[Path]:
    candidates: list[Path] = []
    patterns = [
        "coverage.json",
        "coverage/coverage-final.json",
        "coverage/coverage.json",
        "target/site/jacoco/jacoco.xml",
        "build/reports/jacoco/test/jacocoTestReport.xml",
    ]
    search_roots = _coverage_search_roots(workspace, repos)
    for root in search_roots:
        for pattern in patterns:
            path = root / pattern
            if path.exists() and not _is_maven_target_coverage_json(path):
                candidates.append(path)
        for pattern in _JACOCO_GLOBS:
            candidates.extend(root.glob(pattern))
        for path in root.glob("**/coverage.json"):
            if _is_maven_target_coverage_json(path):
                continue
            candidates.append(path)
    # de-dupe
    seen: set[str] = set()
    out: list[Path] = []
    for path in candidates:
        key = str(path.resolve())
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out[:20]


def _repo_for_report(report: Path, repos: list[Path]) -> str | None:
    resolved = report.resolve()
    for repo in repos:
        try:
            resolved.relative_to(repo.resolve())
            return repo.name
        except ValueError:
            continue
    return None


def _parse_one_report(report: Path) -> CoverageSnapshot | None:
    name = report.name.lower()
    try:
        if name.endswith(".json") and "coverage" in str(report).lower():
            text = report.read_text(encoding="utf-8", errors="ignore")[:200]
            if '"statementMap"' in text or '"s":' in text:
                return parse_istanbul_json(report)
            return parse_coverage_py_json(report)
        if name.endswith(".xml"):
            return parse_jacoco_xml(report)
    except (
        OSError,
        json.JSONDecodeError,
        ElementTree.ParseError,
        KeyError,
        TypeError,
        ValueError,
        AttributeError,
    ):
        return None
    return None


def _prefix_path(path: str, repo_name: str | None, repo_names: set[str]) -> str:
    normalized = path.replace("\\", "/")
    if not repo_name:
        return normalized
    # Already prefixed with a known repo
    for name in repo_names:
        if normalized == name or normalized.startswith(name + "/"):
            return normalized
    return f"{repo_name}/{normalized}"


def load_coverage_snapshot(workspace: Path, repos: list[Path]) -> CoverageSnapshot | None:
    """Parse and merge all discoverable coverage reports across repos."""
    reports = discover_coverage_reports(workspace, repos)
    if not reports:
        return None

    repo_names = {r.name for r in repos}
    repos_with_report: set[str] = set()
    by_path: dict[str, CoverageFileStat] = {}
    sources: list[str] = []
    covered_sum = 0
    total_sum = 0

    for report in reports:
        snap = _parse_one_report(report)
        if snap is None:
            continue
        repo_name = _repo_for_report(report, repos)
        if repo_name:
            repos_with_report.add(repo_name)
        sources.append(snap.source)
        for stat in snap.files:
            key = _prefix_path(stat.path, repo_name, repo_names)
            # Last non-empty wins per normalized path
            by_path[key] = CoverageFileStat(
                path=key,
                covered_lines=stat.covered_lines,
                total_lines=stat.total_lines,
                percent=stat.percent,
            )

    if not by_path:
        return None

    files = list(by_path.values())
    for stat in files:
        covered_sum += stat.covered_lines
        total_sum += stat.total_lines
    overall = (100.0 * covered_sum / total_sum) if total_sum else None

    missing = sorted(name for name in repo_names if name not in repos_with_report)
    source = "merged" if len(set(sources)) > 1 or len(reports) > 1 else (sources[0] if sources else "unknown")
    return CoverageSnapshot(
        summary=f"Merged {len(reports)} coverage report(s); {len(files)} files",
        overall_percent=overall,
        files=files,
        source=source,
        repos_without_coverage=missing,
    )
