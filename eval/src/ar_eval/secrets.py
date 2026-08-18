"""Load local secret files into the process environment (never committed)."""

from __future__ import annotations

import os
from pathlib import Path


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in {"'", '"'}:
            val = val[1:-1]
        if key:
            out[key] = val
    return out


def load_secret_files(*paths: Path, override: bool = False) -> list[Path]:
    """
    Parse KEY=VALUE files and set missing keys in ``os.environ``.

    When ``override`` is False (default), existing environment values win.
    Returns paths that were read.
    """
    loaded: list[Path] = []
    for path in paths:
        if not path.is_file():
            continue
        loaded.append(path)
        for key, val in _parse_env_file(path).items():
            if override or key not in os.environ or not str(os.environ.get(key, "")).strip():
                os.environ[key] = val
    return loaded


def resolve_cursor_api_key(
    *,
    repo_root: Path | None = None,
    eval_dir: Path | None = None,
    explicit: str | None = None,
) -> str:
    """
    Resolve Cursor API key: explicit → env → eval/secrets.local.env → repo .env.
    """
    if explicit and explicit.strip():
        return explicit.strip()
    env_val = (os.environ.get("CURSOR_API_KEY") or "").strip()
    if env_val:
        return env_val

    root = repo_root
    if root is None and eval_dir is not None:
        root = eval_dir.parent
    if root is None:
        root = Path.cwd()
    ev = eval_dir or (root / "eval")

    load_secret_files(ev / "secrets.local.env", root / ".env", override=False)
    return (os.environ.get("CURSOR_API_KEY") or "").strip()
