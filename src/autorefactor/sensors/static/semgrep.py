from __future__ import annotations

import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from autorefactor.analyzers.base import AnalyzerContext
from autorefactor.domain import (
    AnalysisGoal,
    Finding,
    Severity,
    SuggestedAction,
    content_hash,
)
from autorefactor.enrich.unit_match import units_covering_line


class SemgrepAnalyzer:
    name = "semgrep"

    def available(self, ctx: AnalyzerContext) -> bool:
        return shutil.which("semgrep") is not None

    def _configs_for(self, ctx: AnalyzerContext) -> list[str]:
        """Language-aware defaults so Java trees get p/java, not only p/ci."""
        explicit = (ctx.config.semgrep_config or "").strip()
        if explicit:
            return [c.strip() for c in explicit.split(",") if c.strip()]
        langs = {str(getattr(u, "language", "") or "").lower() for u in ctx.units}
        configs = ["p/ci"]
        if "java" in langs:
            configs.append("p/java")
        if "python" in langs:
            configs.append("p/python")
        if langs & {"typescript", "tsx", "javascript", "js"}:
            configs.append("p/javascript")
        if "go" in langs:
            configs.append("p/golang")
        # de-dupe preserve order
        seen: set[str] = set()
        out: list[str] = []
        for c in configs:
            if c not in seen:
                seen.add(c)
                out.append(c)
        return out

    def analyze(self, ctx: AnalyzerContext) -> list[Finding]:
        n_units = len(ctx.units)
        n_repos = len(ctx.repos)
        # Very large trees: full rule packs × every repo can exceed the job-stale
        # window even in parallel. Skip external semgrep; structural sensors already
        # cover the cross-repo signal the user cares about.
        large = n_units >= 20_000 or n_repos >= 24
        if large:
            ctx.note(
                self.name,
                "skip",
                f"skipped on large workspace ({n_repos} repos, {n_units} units); "
                "re-run with focus on a single repo for semgrep",
            )
            return []
        configs = self._configs_for(ctx)
        per_repo_timeout = 240
        flags: list[Finding] = []

        def _run_repo(repo: Path) -> list[Finding]:
            cmd = ["semgrep", "--json", "--quiet"]
            for cfg in configs:
                cmd.extend(["--config", cfg])
            cmd.append(str(repo))
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=per_repo_timeout,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                ctx.note(self.name, "error", str(exc))
                return []
            if not proc.stdout.strip():
                return []
            try:
                payload = json.loads(proc.stdout)
            except json.JSONDecodeError:
                ctx.note(self.name, "error", "invalid JSON from semgrep")
                return []
            repo_flags: list[Finding] = []
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
                repo_flags.append(
                    Finding(
                        id=content_hash(f"semgrep:{check_id}:{rel}:{start}:{message[:80]}"),
                        goal=AnalysisGoal.STATIC, sensor='static',
                        severity=severity,
                        title=f"Semgrep {check_id}",
                        evidence=f"{rel}:{start} — {message}",
                        unit_ids=unit_ids,
                        action_hint=SuggestedAction.DOCUMENT
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
            return repo_flags

        # Parallel per-repo runs keep wall-clock bounded vs the job-stale window.
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_run_repo, repo): repo for repo in ctx.repos}
            for future in as_completed(futures):
                flags.extend(future.result() or [])
        return flags
