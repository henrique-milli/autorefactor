"""Fail-soft coverage report generation during analyze."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

COVERAGE_GENERATE_TIMEOUT_S = 600.0


def coverage_generate_argv(repo: Path) -> list[str] | None:
    """Command to produce Jacoco XML (or None if no runner is available)."""
    repo = repo.resolve()
    mvnw = repo / "mvnw"
    if mvnw.is_file():
        return [str(mvnw), "-q", "test", "jacoco:report"]
    if (repo / "pom.xml").is_file() and shutil.which("mvn"):
        return ["mvn", "-q", "test", "jacoco:report"]
    gradlew = repo / "gradlew"
    if gradlew.is_file():
        return [str(gradlew), "test", "jacocoTestReport"]
    if (
        (repo / "build.gradle").is_file() or (repo / "build.gradle.kts").is_file()
    ) and shutil.which("gradle"):
        return ["gradle", "test", "jacocoTestReport"]
    return None


def _run_cmd(
    argv: list[str],
    *,
    cwd: Path,
    timeout_s: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )


def try_generate_coverage_reports(
    workspace: Path,
    repos: list[Path],
    *,
    timeout_s: float = COVERAGE_GENERATE_TIMEOUT_S,
    run_cmd: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    """If coverage tooling is configured and no XML exists, run the toolchain once.

    Never raises. Returns ``{ran, ok, notes, commands}``.
    """
    from autorefactor.sensors.coverage import coverage_tool_configured

    runner = run_cmd or _run_cmd
    notes: list[str] = []
    commands: list[str] = []
    ran = 0
    ok = 0
    for repo in repos:
        if not coverage_tool_configured(repo):
            continue
        argv = coverage_generate_argv(repo)
        if not argv:
            notes.append(
                f"{repo.name}: coverage tool configured but mvn/gradle not on PATH"
            )
            continue
        ran += 1
        cmd_s = " ".join(argv)
        commands.append(f"{repo.name}: {cmd_s}")
        try:
            proc = runner(argv, cwd=repo, timeout_s=timeout_s)
        except subprocess.TimeoutExpired:
            notes.append(f"{repo.name}: {cmd_s} timed out after {int(timeout_s)}s")
            continue
        except OSError as exc:
            notes.append(f"{repo.name}: {cmd_s} failed to start ({exc})")
            continue
        if proc.returncode == 0:
            ok += 1
            continue
        err = ((proc.stderr or proc.stdout or "")[-400:]).strip()
        notes.append(
            f"{repo.name}: {cmd_s} exited {proc.returncode}"
            + (f": {err}" if err else "")
        )
    return {"ran": ran, "ok": ok, "notes": notes, "commands": commands}
