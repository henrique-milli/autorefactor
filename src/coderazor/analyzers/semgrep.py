from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from coderazor.analyzers.base import AnalyzerContext
from coderazor.analyzers.unit_match import units_covering_line
from coderazor.models import Flag, FlagSource, Severity, SuggestedAction, content_hash


class SemgrepAnalyzer:
    name = "semgrep"

    def available(self, ctx: AnalyzerContext) -> bool:
        return shutil.which("semgrep") is not None

    def analyze(self, ctx: AnalyzerContext) -> list[Flag]:
        config = ctx.config.semgrep_config or "p/ci"
        flags: list[Flag] = []
        for repo in ctx.repos:
            cmd = [
                "semgrep",
                "--json",
                "--quiet",
                "--config",
                config,
                str(repo),
            ]
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=300,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                ctx.note(self.name, "error", str(exc))
                continue
            if not proc.stdout.strip():
                continue
            try:
                payload = json.loads(proc.stdout)
            except json.JSONDecodeError:
                ctx.note(self.name, "error", "invalid JSON from semgrep")
                continue
            for result in payload.get("results", []):
                path = str(result.get("path") or "")
                try:
                    rel = str(Path(path).resolve().relative_to(ctx.workspace.resolve()))
                except ValueError:
                    rel = path
                start = (result.get("start") or {}).get("line") or 0
                check_id = result.get("check_id") or "semgrep"
                message = result.get("extra", {}).get("message") or check_id
                matched = units_covering_line(ctx.units, rel, int(start))
                unit_ids = [u.id for u in matched] or []
                severity_raw = str(
                    (result.get("extra") or {}).get("severity")
                    or (result.get("extra") or {}).get("metadata", {}).get("severity")
                    or "MEDIUM"
                ).upper()
                severity = {
                    "ERROR": Severity.HIGH,
                    "HIGH": Severity.HIGH,
                    "WARNING": Severity.MEDIUM,
                    "MEDIUM": Severity.MEDIUM,
                    "INFO": Severity.LOW,
                    "LOW": Severity.LOW,
                }.get(severity_raw, Severity.MEDIUM)
                flags.append(
                    Flag(
                        id=content_hash(f"semgrep:{check_id}:{rel}:{start}:{message[:80]}"),
                        source=FlagSource.ANALYZER,
                        severity=severity,
                        title=f"Semgrep {check_id}",
                        evidence=f"{rel}:{start} — {message}",
                        unit_ids=unit_ids,
                        suggested_action=SuggestedAction.DOCUMENT
                        if severity == Severity.LOW
                        else SuggestedAction.SPLIT,
                        score=0.55 if severity != Severity.HIGH else 0.8,
                        metadata={
                            "analyzer": self.name,
                            "check_id": check_id,
                            "path": rel,
                            "line": start,
                        },
                    )
                )
        return flags
