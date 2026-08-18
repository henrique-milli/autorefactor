"""Materialize episode roots into an isolated subject directory."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ar_eval.corpus import Episode, resolve_root

FORCE_WALK_MARKER = ".ar_eval_force_walk"


def _ensure_nested_git(workspace: Path) -> None:
    """
    Make the episode dir its own git repo so engine sync packs files.

    Episode paths live under a gitignored ``subjects/`` tree; without a nested
    repo, ``git ls-files`` from the parent project returns an empty set.
    """
    subprocess.run(
        ["git", "init"],
        cwd=str(workspace),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "add", "-A"],
        cwd=str(workspace),
        check=True,
        capture_output=True,
    )


def materialize_episode(
    episode: Episode,
    *,
    repo_root: Path,
    subjects_dir: Path,
    mode: str = "copy",
) -> Path:
    """
    Create ``subjects_dir/<episode.id>/`` with one subdirectory per root.

    Multi-root episodes become sibling dirs named after each root's basename
    (disambiguated with a numeric suffix on collision).

    ``symlink`` mode writes ``.ar_eval_force_walk`` so sync packs follow links
    (needed for large host checkouts).
    """
    dest = subjects_dir / episode.id
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    used_names: set[str] = set()
    for root in episode.roots:
        src = resolve_root(root, repo_root=repo_root)
        if not src.exists():
            raise FileNotFoundError(f"episode {episode.id}: root not found: {src}")
        name = src.name
        if name in used_names:
            i = 2
            while f"{name}-{i}" in used_names:
                i += 1
            name = f"{name}-{i}"
        used_names.add(name)
        target = dest / name
        if mode == "symlink":
            target.symlink_to(src.resolve(), target_is_directory=src.is_dir())
        else:
            if src.is_dir():
                shutil.copytree(
                    src,
                    target,
                    ignore=shutil.ignore_patterns(
                        ".git",
                        "node_modules",
                        ".venv",
                        "venv",
                        "target",
                        "build",
                        ".next",
                        "__pycache__",
                    ),
                    dirs_exist_ok=False,
                )
            else:
                shutil.copy2(src, target)

    if mode == "symlink":
        (dest / FORCE_WALK_MARKER).write_text("1\n", encoding="utf-8")
    else:
        _ensure_nested_git(dest)
    return dest
