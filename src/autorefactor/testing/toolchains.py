from __future__ import annotations

from pathlib import Path

from autorefactor.config import AutorefactorConfig


def detect_repo_test_command(repo: Path) -> str | None:
    if (repo / "pom.xml").exists():
        return "mvn -q test"
    if (repo / "build.gradle").exists() or (repo / "build.gradle.kts").exists():
        wrapper = repo / "gradlew"
        return f"{wrapper} test" if wrapper.exists() else "gradle test"
    if (repo / "package.json").exists():
        if (repo / "pnpm-lock.yaml").exists():
            return "pnpm test"
        if (repo / "yarn.lock").exists():
            return "yarn test"
        return "npm test"
    if (repo / "pyproject.toml").exists() or (repo / "pytest.ini").exists() or (
        repo / "setup.cfg"
    ).exists():
        return "pytest -q"
    if (repo / "go.mod").exists():
        return "go test ./..."
    return None


def detect_workspace_test_command(repos: list[Path], config: AutorefactorConfig) -> str:
    if config.test_cmd:
        return config.test_cmd
    if config.validate_cmd:
        return config.validate_cmd
    commands: list[str] = []
    for repo in repos:
        cmd = detect_repo_test_command(repo)
        if cmd:
            # Scope to repo directory
            commands.append(f"(cd {repo.name} && {cmd})")
    if not commands:
        return "pytest -q"
    if len(commands) == 1:
        return commands[0]
    return " && ".join(commands[:8])


def coverage_augmented_command(base_cmd: str, repos: list[Path]) -> str:
    """Best-effort wrap common runners with coverage collection."""
    import shutil

    if "pytest" in base_cmd and "coverage" not in base_cmd and shutil.which("coverage"):
        return (
            base_cmd.replace("pytest", "coverage run -m pytest", 1)
            + " && coverage json -o coverage.json"
        )
    # pnpm FIRST: "npm test" is a substring of "pnpm test" and would mis-fire.
    if "pnpm test" in base_cmd and "c8" not in base_cmd:
        return base_cmd.replace(
            "pnpm test",
            "pnpm exec c8 --reporter=json --reporter=text pnpm test",
            1,
        )
    if "npm test" in base_cmd and "c8" not in base_cmd and shutil.which("npx"):
        return base_cmd.replace(
            "npm test",
            "npx --yes c8 --reporter=json-summary --reporter=json npm test",
            1,
        )
    if "mvn" in base_cmd and "jacoco" not in base_cmd:
        return base_cmd + " jacoco:report"
    return base_cmd

