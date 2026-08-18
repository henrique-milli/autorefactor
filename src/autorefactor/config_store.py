from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from autorefactor.config import MISSING_API_KEY_HINT, AutorefactorConfig
from autorefactor.paths import autorefactor_home, default_run_dir


SETUP_FIELDS = (
    "chat_backend",
    "embed_backend",
    "embed_device",
    "local_chat",
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
    return autorefactor_home() / "config.toml"


def workspace_config_path(workspace: Path) -> Path:
    return workspace.resolve() / ".autorefactor.toml"


def load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return data if isinstance(data, dict) else {}


def save_toml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# autorefactor configuration", ""]
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


def apply_dict_to_config(config: AutorefactorConfig, data: dict[str, Any]) -> AutorefactorConfig:
    mapping = {
        "workspace": "workspace",
        "out_dir": "out_dir",
        "chat_backend": "chat_backend",
        "embed_backend": "embed_backend",
        "embed_device": "embed_device",
        "local_chat": "local_chat",
        "base_url": "base_url",
        "api_key": "api_key",
        "llm_api_key": "llm_api_key",
        "llm_base_url": "llm_base_url",
        "llm_model": "llm_model",
        "embed_api_key": "embed_api_key",
        "embed_base_url": "embed_base_url",
        "embed_model": "embed_model",
        "embed_concurrency": "embed_concurrency",
        "local_llm_model": "local_llm_model",
        "local_embed_model": "local_embed_model",
        "local_embed_gpu_model": "local_embed_gpu_model",
        "ollama_base_url": "ollama_base_url",
        "describe_mode": "describe_mode",
        "confidence_threshold": "confidence_threshold",
        "focus": "focus",
        "sensors": "sensors",
        "profile": "profile",
        "exclude_repos": "exclude_repos",
        "similarity_threshold": "similarity_threshold",
        "describe_concurrency": "describe_concurrency",
        "provider": "provider",
        "skip_llm": "skip_llm",
        "skip_describe": "skip_describe",
        "max_nesting_depth": "max_nesting_depth",
        "max_cognitive_complexity": "max_cognitive_complexity",
        "max_plan_component_size": "max_plan_component_size",
        "max_enrich_describe": "max_enrich_describe",
        "semgrep_config": "semgrep_config",
    }
    updates: dict[str, Any] = {}
    for src, dest in mapping.items():
        if src in data and data[src] is not None:
            val = data[src]
            if dest in {"workspace", "out_dir"}:
                val = Path(val)
            if dest == "local_chat" and isinstance(val, str):
                val = val.strip().lower() in {"1", "true", "yes", "on"}
            updates[dest] = val
    return config.model_copy(update=updates)


def load_merged_config(
    *,
    workspace: Path | None = None,
    out_dir: Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
) -> AutorefactorConfig:
    user = load_toml(default_user_config_path())
    ws = workspace or Path(user.get("workspace", Path.cwd()))
    ws = Path(ws)
    local = load_toml(workspace_config_path(ws))
    merged = merge_config_dicts(user, local, cli_overrides or {})
    if workspace is not None:
        merged["workspace"] = str(workspace)
    if out_dir is not None:
        merged["out_dir"] = str(out_dir)
    elif "out_dir" not in merged:
        merged["out_dir"] = str(default_run_dir(ws))
    config = AutorefactorConfig()
    config = apply_dict_to_config(config, merged)
    return config.apply_backend_defaults()


def get_setup_requirements(config: AutorefactorConfig | None = None) -> list[dict[str, str]]:
    cfg = (config or AutorefactorConfig()).apply_backend_defaults()
    if cfg.skip_llm:
        return []
    missing: list[dict[str, str]] = []

    if cfg.resolved_chat_backend() == "agent" and (
        cfg.uses_local_embed() or cfg.resolved_embed_axis() == "none"
    ):
        return []
    if cfg.uses_local_chat() and cfg.uses_local_embed():
        return []

    if cfg.resolved_chat_backend() == "http" and not cfg.uses_local_chat():
        try:
            cfg.resolve_llm_api_key()
        except RuntimeError:
            missing.append(
                {
                    "field": "api_key",
                    "question": "API key for chat completions (OpenAI-compatible)?",
                    "reason": MISSING_API_KEY_HINT,
                }
            )
    if cfg.resolved_embed_axis() == "http":
        try:
            cfg.resolve_embed_api_key()
        except RuntimeError:
            missing.append(
                {
                    "field": "api_key",
                    "question": "API key for embeddings (can be same as chat)?",
                    "reason": MISSING_API_KEY_HINT,
                }
            )
    # Deduplicate identical reasons
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for item in missing:
        key = item["field"] + item["reason"]
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def needs_setup(config: AutorefactorConfig) -> bool:
    if config.skip_llm:
        return False
    if config.resolved_chat_backend() == "agent" and config.uses_local_embed():
        return False
    if config.uses_local_chat() and config.uses_local_embed():
        return False
    return bool(get_setup_requirements(config))


def setup_hint(config: AutorefactorConfig | None = None) -> str | None:
    cfg = (config or AutorefactorConfig()).apply_backend_defaults()
    if not needs_setup(cfg):
        return None
    return MISSING_API_KEY_HINT


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
