from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from autorefactor.analyzers.base import AnalyzerContext
from autorefactor.enrich.unit_match import units_covering_line
from autorefactor.domain import AnalysisGoal, Finding, Severity, SuggestedAction, content_hash


class RuffAnalyzer:
    name = "ruff"

    def available(self, ctx: AnalyzerContext) -> bool:
        return shutil.which("ruff") is not None

    def analyze(self, ctx: AnalyzerContext) -> list[Finding]:
        flags: list[Finding] = []
        for repo in ctx.repos:
            py_files = list(repo.rglob("*.py"))
            if not py_files:
                continue
            cmd = ["ruff", "check", "--output-format", "json", str(repo)]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=180)
            except (OSError, subprocess.TimeoutExpired) as exc:
                ctx.note(self.name, "error", str(exc))
                continue
            raw = proc.stdout.strip()
            if not raw:
                continue
            try:
                items = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(items, list):
                continue
            for item in items:
                filename = str(item.get("filename") or "")
                try:
                    rel = str(Path(filename).resolve().relative_to(ctx.workspace.resolve()))
                except ValueError:
                    rel = filename
                loc = item.get("location") or {}
                line = int(loc.get("row") or 0)
                code = item.get("code") or "RUFF"
                message = item.get("message") or code
                matched = units_covering_line(ctx.units, rel, line)
                flags.append(
                    Finding(
                        id=content_hash(f"ruff:{code}:{rel}:{line}:{message[:60]}"),
                        goal=AnalysisGoal.STATIC, sensor='static',
                        severity=Severity.MEDIUM,
                        title=f"Ruff {code}",
                        evidence=f"{rel}:{line} — {message}",
                        unit_ids=[u.id for u in matched],
                        action_hint=SuggestedAction.SPLIT,
                        score=0.5,
                        metadata={"analyzer": self.name, "code": code, "path": rel, "line": line},
                    )
                )
        return flags
