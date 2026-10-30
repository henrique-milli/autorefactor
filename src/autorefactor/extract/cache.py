"""File-tree fingerprint + sqlite extract reuse for incremental analyze."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from autorefactor.domain import CodeUnit
from autorefactor.extract.registry import extract_repo

if TYPE_CHECKING:
    from autorefactor.index.store import IndexStore

_SKIP_DIR = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "target",
    "build",
    "dist",
    ".venv",
    "venv",
    "__pycache__",
    ".autorefactor",
    ".idea",
    ".gradle",
}


def repo_fingerprint(repo: Path) -> str:
    """Stable-ish fingerprint from relative paths + size + mtime (fast, pruned walk).

    Prunes skip/dot directories *while* walking: an rglob-then-filter walk still
    enumerates node_modules (seconds on real workspaces) just to discard it.
    Hashed file set and ordering are identical to the old filter semantics.
    """
    h = hashlib.sha256()
    root = repo.resolve()
    if not root.is_dir():
        return h.hexdigest()
    paths: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    # pathlib.rglob does not descend symlinked dirs; a symlink to a
                    # file still counts (stat follows the target).
                    if entry.is_file():
                        paths.append(entry)
                elif entry.is_dir():
                    if entry.name in _SKIP_DIR or entry.name.startswith("."):
                        continue
                    stack.append(entry)
                elif entry.is_file():
                    rel_parts = entry.relative_to(root).parts
                    if any(p in _SKIP_DIR or p.startswith(".") for p in rel_parts[:-1]):
                        continue
                    paths.append(entry)
            except OSError:
                continue
    for path in sorted(paths):
        try:
            st = path.stat()
        except OSError:
            continue
        h.update(str(path.relative_to(root)).encode("utf-8", errors="replace"))
        h.update(b"\0")
        h.update(str(st.st_size).encode())
        h.update(b"\0")
        h.update(str(st.st_mtime_ns).encode())
        h.update(b"\n")
    return h.hexdigest()


def extract_workspace_cached(
    repos: list[Path],
    store: IndexStore | None = None,
) -> tuple[list[CodeUnit], dict[str, str]]:
    """Extract with optional per-repo cache. Returns (units, stats)."""
    units: list[CodeUnit] = []
    stats = {"repos": str(len(repos)), "cache_hits": "0", "cache_misses": "0"}
    hits = misses = 0
    for repo in repos:
        name = repo.name
        fp = repo_fingerprint(repo)
        if store is not None:
            cached = store.get_extract_cache(name, fp)
            if cached is not None:
                try:
                    units.extend(CodeUnit.model_validate(u) for u in cached)
                    hits += 1
                    continue
                except Exception:  # noqa: BLE001
                    pass
        extracted = extract_repo(repo)
        units.extend(extracted)
        misses += 1
        if store is not None:
            store.put_extract_cache(
                name, fp, [u.model_dump(mode="json") for u in extracted]
            )
    stats["cache_hits"] = str(hits)
    stats["cache_misses"] = str(misses)
    return units, stats
