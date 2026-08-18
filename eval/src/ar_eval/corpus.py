"""Load and merge corpus YAML (example + optional local overlay)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Episode:
    id: str
    roots: list[str]


@dataclass(frozen=True)
class Corpus:
    episodes: list[Episode]
    source_paths: list[Path]


def _parse_episodes(raw: Any) -> list[Episode]:
    if not isinstance(raw, dict):
        raise ValueError("corpus root must be a mapping")
    items = raw.get("episodes") or []
    if not isinstance(items, list):
        raise ValueError("episodes must be a list")
    out: list[Episode] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"episodes[{i}] must be a mapping")
        eid = str(item.get("id") or "").strip()
        if not eid:
            raise ValueError(f"episodes[{i}] missing id")
        roots = item.get("roots") or []
        if not isinstance(roots, list) or not roots:
            raise ValueError(f"episodes[{eid}] roots must be a non-empty list")
        out.append(Episode(id=eid, roots=[str(r) for r in roots]))
    return out


def load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: root must be a mapping")
    return data


def load_corpus(
    *,
    eval_dir: Path,
    example_name: str = "corpus.example.yaml",
    local_name: str = "corpus.local.yaml",
    use_local: bool = True,
) -> Corpus:
    """Load example corpus; if local overlay exists and use_local, replace episodes."""
    example = eval_dir / example_name
    if not example.is_file():
        raise FileNotFoundError(f"missing corpus example: {example}")
    sources = [example]
    data = load_yaml(example)
    episodes = _parse_episodes(data)

    local = eval_dir / local_name
    if use_local and local.is_file():
        sources.append(local)
        local_data = load_yaml(local)
        # Local wins entirely for episodes when present and non-empty.
        if local_data.get("episodes"):
            episodes = _parse_episodes(local_data)

    return Corpus(episodes=episodes, source_paths=sources)


def resolve_root(root: str, *, repo_root: Path) -> Path:
    """Resolve a corpus root: absolute stays absolute; relative is under repo_root."""
    p = Path(root).expanduser()
    if p.is_absolute():
        return p.resolve()
    return (repo_root / p).resolve()
