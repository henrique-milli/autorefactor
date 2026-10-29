from __future__ import annotations

from pathlib import Path

from autorefactor.config import AutorefactorConfig


def _write(root: Path, rel: str, text: str = "{}") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_pnpm_workspace_root_is_single_repo(tmp_path: Path) -> None:
    """Monorepo roots (pnpm-workspace.yaml) must analyze as ONE project.

    Regression: docable's agents/pyproject.toml made discover_repos return only
    the stray marker-bearing child, silently dropping apps/ + supabase/.
    """
    _write(tmp_path, "pnpm-workspace.yaml", "packages:\n  - apps/*\n")
    _write(tmp_path, "package.json")
    _write(tmp_path, "apps/web/package.json")  # marker at depth 2 only
    _write(tmp_path, "agents/pyproject.toml")  # marker at depth 1 (the trap)
    _write(tmp_path, "supabase/migrations/0001.sql", "select 1;")

    repos = AutorefactorConfig(workspace=tmp_path).discover_repos()
    assert repos == [tmp_path]


def test_lerna_workspace_root_is_single_repo(tmp_path: Path) -> None:
    _write(tmp_path, "lerna.json")
    _write(tmp_path, "package.json")
    _write(tmp_path, "packages/core/package.json")

    repos = AutorefactorConfig(workspace=tmp_path).discover_repos()
    assert repos == [tmp_path]


def test_multi_repo_root_returns_git_children(tmp_path: Path) -> None:
    _write(tmp_path, ".git/HEAD", "ref: refs/heads/main\n")
    _write(tmp_path, "alpha/.git/HEAD", "ref: refs/heads/main\n")
    _write(tmp_path, "beta/package.json")
    _write(tmp_path, "gamma/.git/HEAD", "ref: refs/heads/main\n")

    repos = AutorefactorConfig(workspace=tmp_path).discover_repos()
    # .git or package markers qualify a child; unmarked dirs are not repos.
    assert [r.name for r in repos] == ["alpha", "beta", "gamma"]


def test_single_root_without_marked_children(tmp_path: Path) -> None:
    _write(tmp_path, "package.json")
    _write(tmp_path, "src/main.ts", "export {};\n")

    repos = AutorefactorConfig(workspace=tmp_path).discover_repos()
    assert repos == [tmp_path]
