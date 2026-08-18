from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from xml.etree import ElementTree

from autorefactor.analyzers.base import AnalyzerContext
from autorefactor.enrich.unit_match import units_covering_line
from autorefactor.domain import AnalysisGoal, Finding, Severity, SuggestedAction, content_hash


class JavaStaticAnalyzer:
    """PMD preferred; SpotBugs XML report import as fallback when present."""

    name = "java_static"

    def available(self, ctx: AnalyzerContext) -> bool:
        has_java = any(
            (repo / "pom.xml").exists()
            or (repo / "build.gradle").exists()
            or (repo / "build.gradle.kts").exists()
            or list(repo.glob("**/*.java"))[:1]
            for repo in ctx.repos
        )
        if not has_java:
            return False
        return (
            shutil.which("pmd") is not None
            or shutil.which("spotbugs") is not None
            or any(repo.rglob("**/spotbugsXml.xml") for repo in ctx.repos)
            or any(repo.rglob("**/pmd.xml") for repo in ctx.repos)
        )

    def analyze(self, ctx: AnalyzerContext) -> list[Finding]:
        flags: list[Finding] = []
        if shutil.which("pmd"):
            flags.extend(self._run_pmd(ctx))
        flags.extend(self._import_existing_reports(ctx))
        return flags

    def _run_pmd(self, ctx: AnalyzerContext) -> list[Finding]:
        flags: list[Finding] = []
        for repo in ctx.repos:
            if not list(repo.rglob("*.java"))[:1]:
                continue
            out = ctx.config.resolve_cache_dir() / f"pmd-{repo.name}.xml"
            cmd = [
                "pmd",
                "check",
                "-d",
                str(repo),
                "-R",
                "category/java/bestpractices.xml",
                "-f",
                "xml",
                "-r",
                str(out),
            ]
            try:
                subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=300)
            except (OSError, subprocess.TimeoutExpired) as exc:
                ctx.note(self.name, "error", f"pmd: {exc}")
                continue
            if out.exists():
                flags.extend(self._parse_pmd_xml(ctx, out))
        return flags

    def _import_existing_reports(self, ctx: AnalyzerContext) -> list[Finding]:
        flags: list[Finding] = []
        for repo in ctx.repos:
            for report in list(repo.rglob("pmd.xml"))[:5]:
                flags.extend(self._parse_pmd_xml(ctx, report))
            for report in list(repo.rglob("spotbugsXml.xml"))[:5]:
                flags.extend(self._parse_spotbugs_xml(ctx, report))
        return flags

    def _parse_pmd_xml(self, ctx: AnalyzerContext, path: Path) -> list[Finding]:
        flags: list[Finding] = []
        try:
            root = ElementTree.parse(path).getroot()
        except ElementTree.ParseError:
            return flags
        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}")[0] + "}"
        for file_el in root.findall(f".//{ns}file"):
            filename = file_el.attrib.get("name") or ""
            try:
                rel = str(Path(filename).resolve().relative_to(ctx.workspace.resolve()))
            except ValueError:
                rel = filename
            for violation in file_el.findall(f"{ns}violation"):
                line = int(violation.attrib.get("beginline") or 0)
                rule = violation.attrib.get("rule") or "pmd"
                text = (violation.text or rule).strip()
                matched = units_covering_line(ctx.units, rel, line)
                flags.append(
                    Finding(
                        id=content_hash(f"pmd:{rule}:{rel}:{line}:{text[:60]}"),
                        goal=AnalysisGoal.STATIC, sensor='static',
                        severity=Severity.MEDIUM,
                        title=f"PMD {rule}",
                        evidence=f"{rel}:{line} — {text}",
                        unit_ids=[u.id for u in matched],
                        action_hint=SuggestedAction.SPLIT,
                        score=0.5,
                        metadata={"analyzer": "pmd", "rule": rule, "path": rel, "line": line},
                    )
                )
        return flags

    def _parse_spotbugs_xml(self, ctx: AnalyzerContext, path: Path) -> list[Finding]:
        flags: list[Finding] = []
        try:
            root = ElementTree.parse(path).getroot()
        except ElementTree.ParseError:
            return flags
        for bug in root.findall(".//BugInstance"):
            typ = bug.attrib.get("type") or "spotbugs"
            rank = int(bug.attrib.get("rank") or 10)
            source = bug.find(".//SourceLine")
            if source is None:
                continue
            sourcepath = source.attrib.get("sourcepath") or source.attrib.get("classname") or ""
            line = int(source.attrib.get("start") or 0)
            rel_candidates = [u for u in ctx.units if sourcepath.replace(".", "/") in u.path.replace("\\", "/")]
            unit_ids = [u.id for u in rel_candidates[:3]]
            flags.append(
                Finding(
                    id=content_hash(f"spotbugs:{typ}:{sourcepath}:{line}"),
                    goal=AnalysisGoal.STATIC, sensor='static',
                    severity=Severity.HIGH if rank <= 4 else Severity.MEDIUM,
                    title=f"SpotBugs {typ}",
                    evidence=f"{sourcepath}:{line}",
                    unit_ids=unit_ids,
                    action_hint=SuggestedAction.SPLIT,
                    score=0.7 if rank <= 4 else 0.5,
                    metadata={"analyzer": "spotbugs", "type": typ, "line": line},
                )
            )
        return flags
