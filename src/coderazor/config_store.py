from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from coderazor.config import CoderazorConfig


SETUP_FIELDS = (
    "workspace",
    "out_dir",
    "base_url",
    "api_key",
    "llm_model",
    "embed_model",
    "describe_mode",
    "confidence_threshold",
)


def default_user_config_path() -> Path:
    return Path.home() / ".config" / "coderazor" / "config.toml"


def workspace_config_path(workspace: Path) -> Path:
    return workspace.resolve() / ".coderazor.toml"


def load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return data if isinstance(data, dict) else {}


def save_toml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# coderazor configuration", ""]
    for key, value in data.items():
        if value is None:
            continue
        if isinstance(value, bool):
            lines.append(f"{key} = {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key} = {value}")
        else:
            escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{key} = "{escaped}"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def merge_config_dicts(*parts: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for part in parts:
        for key, value in part.items():
            if value is not None and value != "":
                merged[key] = value
    return merged


def apply_dict_to_config(config: CoderazorConfig, data: dict[str, Any]) -> CoderazorConfig:
    mapping = {
        "workspace": "workspace",
        "out_dir": "out_dir",
        "base_url": "base_url",
        "api_key": "api_key",
        "llm_api_key": "llm_api_key",
        "llm_base_url": "llm_base_url",
        "llm_model": "llm_model",
        "embed_api_key": "embed_api_key",
        "embed_base_url": "embed_base_url",
        "embed_model": "embed_model",
        "describe_mode": "describe_mode",
        "confidence_threshold": "confidence_threshold",
        "analyzers": "analyzers",
        "exclude_repos": "exclude_repos",
        "similarity_threshold": "similarity_threshold",
        "describe_concurrency": "describe_concurrency",
        "provider": "provider",
        "skip_llm": "skip_llm",
        "skip_describe": "skip_describe",
    }
    updates: dict[str, Any] = {}
    for src, dest in mapping.items():
        if src in data and data[src] is not None:
            val = data[src]
            if dest in {"workspace", "out_dir"}:
                val = Path(val)
            updates[dest] = val
    return config.model_copy(update=updates)


def load_merged_config(
    *,
    workspace: Path | None = None,
    out_dir: Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
) -> CoderazorConfig:
    user = load_toml(default_user_config_path())
    ws = workspace or Path(user.get("workspace", Path.cwd()))
    ws = Path(ws)
    local = load_toml(workspace_config_path(ws))
    merged = merge_config_dicts(user, local, cli_overrides or {})
    if workspace is not None:
        merged["workspace"] = str(workspace)
    if out_dir is not None:
        merged["out_dir"] = str(out_dir)
    config = CoderazorConfig()
    return apply_dict_to_config(config, merged)


def get_setup_requirements(config: CoderazorConfig | None = None) -> list[dict[str, str]]:
    cfg = config or CoderazorConfig()
    missing: list[dict[str, str]] = []
    mode = cfg.resolved_describe_mode()
    if not cfg.skip_llm and mode in {"index", "write"}:
        try:
            cfg.resolve_llm_api_key()
        except RuntimeError:
            missing.append(
                {
                    "field": "api_key",
                    "question": "API key for chat completions (OpenAI-compatible)?",
                    "reason": "describe_mode requires a chat provider",
                }
            )
    if not cfg.skip_llm:
        try:
            cfg.resolve_embed_api_key()
        except RuntimeError:
            missing.append(
                {
                    "field": "api_key",
                    "question": "API key for embeddings (can be same as chat)?",
                    "reason": "embedding step requires credentials unless --skip-llm",
                }
            )
    if not cfg.llm_model:
        missing.append(
            {"field": "llm_model", "question": "Chat model id?", "reason": "missing llm_model"}
        )
    if not cfg.embed_model:
        missing.append(
            {
                "field": "embed_model",
                "question": "Embedding model id?",
                "reason": "missing embed_model",
            }
        )
    return missing


def save_config(
    partial: dict[str, Any],
    *,
    workspace: Path | None = None,
    user_level: bool = False,
) -> Path:
    if user_level:
        path = default_user_config_path()
    else:
        ws = Path(partial.get("workspace") or workspace or Path.cwd())
        path = workspace_config_path(ws)
    existing = load_toml(path)
    existing.update({k: v for k, v in partial.items() if v is not None})
    save_toml(path, existing)
    return path


def needs_setup(config: CoderazorConfig) -> bool:
    if config.skip_llm:
        return False
    return bool(get_setup_requirements(config))
