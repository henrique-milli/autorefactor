from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from autorefactor.analyzers.base import AnalyzerContext
from autorefactor.enrich.unit_match import units_covering_line
from autorefactor.domain import AnalysisGoal, Finding, Severity, SuggestedAction, content_hash


class EslintAnalyzer:
    name = "eslint"

    def available(self, ctx: AnalyzerContext) -> bool:
        if shutil.which("eslint") is None and not any(
            (repo / "node_modules" / ".bin" / "eslint").exists() for repo in ctx.repos
        ):
            return False
        return any(
            (repo / "package.json").exists()
            or list(repo.glob("**/*.{ts,tsx,js,jsx}"))[:1]
            for repo in ctx.repos
        )

    def _eslint_bin(self, repo: Path) -> str:
        local = repo / "node_modules" / ".bin" / "eslint"
        if local.exists():
            return str(local)
        return "eslint"

    def analyze(self, ctx: AnalyzerContext) -> list[Finding]:
        flags: list[Finding] = []
        for repo in ctx.repos:
            if not (repo / "package.json").exists() and not list(repo.glob("*.ts")):
                # Still try if JS/TS sources exist
                if not any(repo.rglob("*.ts")) and not any(repo.rglob("*.tsx")) and not any(
                    repo.rglob("*.js")
                ):
                    continue
            eslint = self._eslint_bin(repo)
            cmd = [eslint, ".", "-f", "json", "--no-error-on-unmatched-pattern"]
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=repo,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=180,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                ctx.note(self.name, "error", str(exc))
                continue
            raw = proc.stdout.strip()
            if not raw:
                continue
            try:
                files = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(files, list):
                continue
            for file_entry in files:
                filename = str(file_entry.get("filePath") or "")
                try:
                    rel = str(Path(filename).resolve().relative_to(ctx.workspace.resolve()))
                except ValueError:
                    rel = f"{repo.name}/{Path(filename).name}"
                for msg in file_entry.get("messages") or []:
                    line = int(msg.get("line") or 0)
                    rule = msg.get("ruleId") or "eslint"
                    message = msg.get("message") or rule
                    severity = Severity.HIGH if msg.get("severity") == 2 else Severity.MEDIUM
                    matched = units_covering_line(ctx.units, rel, line)
                    flags.append(
                        Finding(
                            id=content_hash(f"eslint:{rule}:{rel}:{line}:{message[:60]}"),
                            goal=AnalysisGoal.STATIC, sensor='static',
                            severity=severity,
                            title=f"ESLint {rule}",
                            evidence=f"{rel}:{line} — {message}",
                            unit_ids=[u.id for u in matched],
                            action_hint=SuggestedAction.SPLIT,
                            score=0.55 if severity == Severity.HIGH else 0.45,
                            metadata={
                                "analyzer": self.name,
                                "rule": rule,
                                "path": rel,
                                "line": line,
                            },
                        )
                    )
        return flags
