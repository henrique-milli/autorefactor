from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from coderazor.analyzers.base import AnalyzerContext
from coderazor.analyzers.unit_match import units_covering_line
from coderazor.models import Flag, FlagSource, Severity, SuggestedAction, content_hash


class RuffAnalyzer:
    name = "ruff"

    def available(self, ctx: AnalyzerContext) -> bool:
        if shutil.which("ruff") is None:
            return False
        return any((repo / "pyproject.toml").exists() or list(repo.glob("**/*.py"))[:1] for repo in ctx.repos)

    def analyze(self, ctx: AnalyzerContext) -> list[Flag]:
        flags: list[Flag] = []
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
                    Flag(
                        id=content_hash(f"ruff:{code}:{rel}:{line}:{message[:60]}"),
                        source=FlagSource.ANALYZER,
                        severity=Severity.MEDIUM,
                        title=f"Ruff {code}",
                        evidence=f"{rel}:{line} — {message}",
                        unit_ids=[u.id for u in matched],
                        suggested_action=SuggestedAction.SPLIT,
                        score=0.5,
                        metadata={"analyzer": self.name, "code": code, "path": rel, "line": line},
                    )
                )
        return flags
