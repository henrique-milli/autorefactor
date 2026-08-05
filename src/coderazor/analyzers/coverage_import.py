from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree

from coderazor.analyzers.base import AnalyzerContext
from coderazor.analyzers.unit_match import units_covering_line
from coderazor.models import (
    CoverageFileStat,
    CoverageSnapshot,
    Flag,
    FlagSource,
    Severity,
    SuggestedAction,
    content_hash,
)


def parse_coverage_py_json(path: Path) -> CoverageSnapshot:
    data = json.loads(path.read_text(encoding="utf-8"))
    files: list[CoverageFileStat] = []
    totals = data.get("totals") or {}
    for file_path, info in (data.get("files") or {}).items():
        summary = info.get("summary") or {}
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
    files: list[CoverageFileStat] = []
    covered_sum = 0
    total_sum = 0
    for file_path, info in data.items():
        if not isinstance(info, dict):
            continue
        statement_map = info.get("statementMap") or {}
        s = info.get("s") or {}
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


def parse_jacoco_xml(path: Path) -> CoverageSnapshot:
    root = ElementTree.parse(path).getroot()
    files: list[CoverageFileStat] = []
    for package in root.findall("package"):
        pkg = package.attrib.get("name", "").replace(".", "/")
        for source in package.findall("sourcefile"):
            name = source.attrib.get("name") or ""
            rel = f"{pkg}/{name}" if pkg else name
            covered = 0
            total = 0
            for counter in source.findall("counter"):
                if counter.attrib.get("type") == "LINE":
                    covered = int(counter.attrib.get("covered") or 0)
                    missed = int(counter.attrib.get("missed") or 0)
                    total = covered + missed
            percent = (100.0 * covered / total) if total else 0.0
            files.append(
                CoverageFileStat(
                    path=rel.replace("\\", "/"),
                    covered_lines=covered,
                    total_lines=total,
                    percent=percent,
                )
            )
    overall = None
    for counter in root.findall("counter"):
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


def discover_coverage_reports(workspace: Path, repos: list[Path]) -> list[Path]:
    candidates: list[Path] = []
    patterns = [
        "coverage.json",
        "coverage/coverage-final.json",
        "coverage/coverage.json",
        "target/site/jacoco/jacoco.xml",
        "build/reports/jacoco/test/jacocoTestReport.xml",
    ]
    search_roots = [workspace, *repos]
    for root in search_roots:
        for pattern in patterns:
            path = root / pattern
            if path.exists():
                candidates.append(path)
        candidates.extend(root.glob("**/coverage.json"))
        candidates.extend(root.glob("**/jacoco.xml"))
    # de-dupe
    seen: set[str] = set()
    out: list[Path] = []
    for path in candidates:
        key = str(path.resolve())
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out[:20]


def load_coverage_snapshot(workspace: Path, repos: list[Path]) -> CoverageSnapshot | None:
    reports = discover_coverage_reports(workspace, repos)
    for report in reports:
        name = report.name.lower()
        try:
            if name.endswith(".json") and "coverage" in str(report).lower():
                text = report.read_text(encoding="utf-8", errors="ignore")[:200]
                if '"statementMap"' in text or '"s":' in text:
                    return parse_istanbul_json(report)
                return parse_coverage_py_json(report)
            if name.endswith(".xml"):
                return parse_jacoco_xml(report)
        except (OSError, json.JSONDecodeError, ElementTree.ParseError, KeyError, TypeError, ValueError):
            continue
    return None


class CoverageImportAnalyzer:
    """Emit coverage-gap flags for poorly covered source files when reports exist."""

    name = "coverage"

    def available(self, ctx: AnalyzerContext) -> bool:
        return bool(discover_coverage_reports(ctx.workspace, ctx.repos))

    def analyze(self, ctx: AnalyzerContext) -> list[Flag]:
        snap = load_coverage_snapshot(ctx.workspace, ctx.repos)
        if snap is None:
            return []
        threshold = ctx.config.coverage_threshold
        flags: list[Flag] = []
        for stat in snap.files:
            if stat.total_lines <= 0:
                continue
            if stat.percent >= threshold:
                continue
            matched = units_covering_line(ctx.units, stat.path, 1)
            # Prefer file-level units
            file_units = [u for u in matched if u.path.replace("\\", "/") in stat.path or stat.path.endswith(u.path.replace("\\", "/"))]
            unit_ids = [u.id for u in (file_units or matched)]
            flags.append(
                Flag(
                    id=content_hash(f"coverage:{stat.path}:{stat.percent}"),
                    source=FlagSource.COVERAGE,
                    severity=Severity.MEDIUM if stat.percent >= threshold / 2 else Severity.HIGH,
                    title=f"Low coverage {stat.path}",
                    evidence=f"{stat.percent:.1f}% lines covered (threshold {threshold}%)",
                    unit_ids=unit_ids,
                    suggested_action=SuggestedAction.ENSURE_TESTS,
                    score=max(0.3, min(1.0, (threshold - stat.percent) / max(threshold, 1))),
                    metadata={
                        "analyzer": self.name,
                        "path": stat.path,
                        "percent": stat.percent,
                        "source": snap.source,
                    },
                )
            )
        return flags
