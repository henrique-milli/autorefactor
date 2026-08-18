from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

IGNORE_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".vscode",
        "node_modules",
        "target",
        "build",
        "dist",
        ".venv",
        "venv",
        "__pycache__",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        "vendor",
        "out",
    }
)

MAX_SOURCE_CHARS = 12_000
DEFAULT_SIMILARITY_THRESHOLD = 0.78

# Core sensors always available without host CLIs
CORE_SENSORS = (
    "sim.method,sim.class,sim.fingerprint,slop,placement,structure,"
    "complexity,coverage,test_quality,contract,cluster,wiring"
)
FULL_SENSORS = CORE_SENSORS + ",static.semgrep,static.ruff"
STATIC_SENSOR_IDS = frozenset(
    {"static.semgrep", "static.ruff", "static.eslint", "static.java"}
)

# OpenAI API (chat + embeddings)
OPENAI_BASE_URL = "https://api.openai.com/v1"
OLLAMA_BASE_URL = "http://ollama:11434/v1"
# 2026 defaults: Gemma 4 E2B (edge chat), EmbeddingGemma (on-device embed), Luna (cloud chat)
DEFAULT_LOCAL_LLM_MODEL = "gemma4:e2b"
DEFAULT_LOCAL_EMBED_MODEL = "nomic-embed-text"
DEFAULT_LOCAL_EMBED_GPU_MODEL = "qwen3-embedding:0.6b"
DEFAULT_CLOUD_LLM_MODEL = "gpt-5.6-luna"
DEFAULT_CLOUD_EMBED_MODEL = "text-embedding-3-small"
DEFAULT_HTTP_EMBED_CONCURRENCY = 4

# Sensors that require real embeddings (stripped when embed_backend=none)
EMBED_DEPENDENT_SENSORS = frozenset({"sim.method", "sim.class", "placement", "cluster"})

VALID_CHAT_BACKENDS = frozenset({"agent", "http", "none"})
VALID_EMBED_BACKENDS = frozenset({"local", "http", "none"})
VALID_EMBED_DEVICES = frozenset({"cpu", "gpu"})

MISSING_API_KEY_HINT = (
    "Missing API key. Add one in Settings (http://localhost:3000/settings), "
    "set AUTOREFACTOR_API_KEY / OPENAI_API_KEY, or switch to local "
    "(agent chat + local embed via Ollama)."
)


class AutorefactorConfig(BaseSettings):
    """Provider credentials are layered:

    1. Role-specific: ``llm_*``, ``embed_*``, ``agent_*``
    2. Shared fallback: ``api_key`` / ``base_url`` / models
    3. Well-known env aliases: ``OPENAI_API_KEY``, ``LLM_API_KEY``

    Topology: ``chat_backend``, ``embed_backend``, ``embed_device``, ``local_chat``,
    ``describe_mode``. Unset axes default to cloud ``http``/``http`` (key required).
    """

    model_config = SettingsConfigDict(
        env_prefix="AUTOREFACTOR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    workspace: Path = Field(default_factory=lambda: Path.cwd())
    out_dir: Path = Field(default_factory=lambda: Path("out"))

    # Axes. None = auto (cloud http/http).
    chat_backend: str | None = None
    embed_backend: str | None = None
    embed_device: str | None = None  # cpu | gpu (local embed model selection)
    # When chat_backend=http, route chat to Ollama
    local_chat: bool | None = None

    # Shared defaults (single-provider mode)
    base_url: str = OPENAI_BASE_URL
    api_key: str | None = None
    llm_model: str = DEFAULT_CLOUD_LLM_MODEL
    embed_model: str = DEFAULT_CLOUD_EMBED_MODEL
    # Local (Ollama) model overrides
    local_llm_model: str = DEFAULT_LOCAL_LLM_MODEL
    local_embed_model: str = DEFAULT_LOCAL_EMBED_MODEL
    local_embed_gpu_model: str = DEFAULT_LOCAL_EMBED_GPU_MODEL
    ollama_base_url: str = OLLAMA_BASE_URL
    # Cap describe enrich tasks enqueued in agent mode
    max_enrich_describe: int = 40

    @field_validator("local_chat", mode="before")
    @classmethod
    def _coerce_local_chat(cls, value: object) -> bool | None:
        """Compose often injects AUTOREFACTOR_LOCAL_CHAT= (empty) — treat as unset."""
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            low = value.strip().lower()
            if low in {"", "none", "null"}:
                return None
            if low in {"1", "true", "yes", "on"}:
                return True
            if low in {"0", "false", "no", "off"}:
                return False
        return value  # type: ignore[return-value]

    @field_validator("chat_backend", "embed_backend", "embed_device", mode="before")
    @classmethod
    def _empty_axis_to_none(cls, value: object) -> object:
        if value is None or value == "":
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    # Optional per-role overrides
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    embed_api_key: str | None = None
    embed_base_url: str | None = None
    agent_api_key: str | None = None
    agent_base_url: str | None = None
    agent_model: str | None = None

    # Convenience: set provider=openai (default) for OpenAI endpoints/models
    provider: str | None = None

    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD
    describe_concurrency: int = 16
    max_retries: int = 10
    # Local Ollama embeddings: smaller batches + longer HTTP timeout (auto when unset)
    embed_batch_size: int | None = None
    embed_concurrency: int | None = None
    embed_http_timeout: float | None = None
    validate_cmd: str | None = None
    test_cmd: str | None = None
    require_tests: bool = True
    require_harness: bool = True
    cache_dir: Path | None = None
    repo_globs: list[str] = Field(default_factory=lambda: ["*"])
    exclude_repos: str = ""  # comma-separated directory names to skip
    long_function_lines: int = 50
    large_file_lines: int = 280
    max_nesting_depth: int = 4
    max_cognitive_complexity: int = 15
    skip_llm: bool = False
    skip_describe: bool = False
    # describe_mode: raw | describe
    describe_mode: str = "raw"
    confidence_threshold: float = 0.6
    # focus: balanced | cross_repo | smells | clones | slop | strict
    focus: str = "balanced"
    max_flags: int | None = None
    max_similarity_pairs: int = 50
    fingerprint_threshold: float = 0.72
    max_fingerprint_pairs: int = 80
    # Max findings per LLM planning brief (oversized conflict components are split)
    max_plan_component_size: int = 8
    queue_display_limit: int = 20
    # sensors=auto|full or comma-separated sensor ids
    sensors: str = "auto"
    # Optional: full | light (also via AUTOREFACTOR_PROFILE)
    profile: str | None = None
    # Optional hint packs (e.g. jvm) — off by default
    hints: str = ""
    semgrep_config: str | None = None
    coverage_threshold: float = 80.0
    # Cap ensure_tests findings per repo (ranked by uncovered LOC)
    max_coverage_findings: int = 15
    document_languages: str = "java,typescript,javascript,python"
    max_test_iterations: int = 2
    config_file: Path | None = None

    def excluded_repo_names(self) -> set[str]:
        return {p.strip() for p in self.exclude_repos.split(",") if p.strip()}

    def resolved_profile(self) -> str:
        raw = (self.profile or os.environ.get("AUTOREFACTOR_PROFILE") or "light").strip().lower()
        if raw in {"full", "complete", "docker"}:
            return "full"
        return "light"

    def enabled_sensors(self) -> str:
        raw = (self.sensors or "auto").strip()
        lowered = raw.lower()
        if lowered == "full" or (lowered in {"", "auto"} and self.resolved_profile() == "full"):
            sensors = FULL_SENSORS
        elif raw and lowered not in {"auto", "*"}:
            sensors = raw
        else:
            sensors = CORE_SENSORS
        if self.resolved_embed_backend() == "none":
            parts = [p.strip() for p in sensors.split(",") if p.strip()]
            parts = [p for p in parts if p not in EMBED_DEPENDENT_SENSORS]
            return ",".join(parts)
        return sensors

    def document_language_set(self) -> set[str]:
        return {p.strip().lower() for p in self.document_languages.split(",") if p.strip()}

    def resolved_chat_axis(self) -> str:
        """User-facing chat axis: agent | http | none."""
        if self.skip_llm:
            return "none"
        raw = (self.chat_backend or "").strip().lower()
        if raw in VALID_CHAT_BACKENDS:
            return raw
        # Auto: cloud HTTP (key required unless local_chat / explicit axes)
        return "http"

    def resolved_embed_axis(self) -> str:
        """User-facing embed axis: local | http | none."""
        if self.skip_llm:
            return "none"
        raw = (self.embed_backend or "").strip().lower()
        if raw in VALID_EMBED_BACKENDS:
            return raw
        return "http"

    def resolved_embed_device(self) -> str:
        raw = (self.embed_device or "").strip().lower()
        if raw in VALID_EMBED_DEVICES:
            return raw
        return "cpu"

    def uses_local_chat(self) -> bool:
        if self.resolved_chat_axis() != "http":
            return False
        if self.local_chat is True:
            return True
        if self.local_chat is False:
            return False
        if self._base_looks_like_ollama() and not self._cloud_base_url():
            return True
        return False

    def uses_local_embed(self) -> bool:
        return self.resolved_embed_axis() == "local"

    def uses_gpu_embed(self) -> bool:
        return self.uses_local_embed() and self.resolved_embed_device() == "gpu"

    def ollama_required(self) -> bool:
        return self.uses_local_embed() or self.uses_local_chat()

    def _cloud_base_url(self) -> bool:
        base = (self.base_url or "").lower()
        return "openai.com" in base or "api.anthropic" in base

    def _base_looks_like_ollama(self) -> bool:
        candidates = (
            self.base_url,
            self.llm_base_url,
            self.embed_base_url,
            os.environ.get("AUTOREFACTOR_BASE_URL"),
            os.environ.get("AUTOREFACTOR_LLM_BASE_URL"),
            os.environ.get("AUTOREFACTOR_EMBED_BASE_URL"),
        )
        for c in candidates:
            if not c:
                continue
            low = str(c).lower()
            if "11434" in low or "ollama" in low:
                return True
        return bool(os.environ.get("AUTOREFACTOR_OLLAMA"))

    @staticmethod
    def _url_looks_cloud(url: str | None) -> bool:
        if not url:
            return False
        low = url.lower()
        return "openai.com" in low or "api.anthropic" in low or "googleapis.com" in low

    def resolved_chat_backend(self) -> str:
        """Pipeline chat transport: agent | http | stub."""
        if self.skip_llm:
            return "stub"
        axis = self.resolved_chat_axis()
        if axis == "none":
            return "stub"
        if axis == "agent":
            return "agent"
        return "http"

    def resolved_embed_backend(self) -> str:
        """Pipeline embed transport: none | http | stub (local Ollama is HTTP)."""
        if self.skip_llm:
            return "stub"
        axis = self.resolved_embed_axis()
        if axis == "none":
            return "none"
        return "http"

    def apply_backend_defaults(self) -> AutorefactorConfig:
        """Return a copy with URL/model defaults applied for resolved axes."""
        updates: dict[str, object] = {}
        cloud_llm_defaults = {
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "gpt-4o-mini",
            "gpt-4o",
            DEFAULT_CLOUD_LLM_MODEL,
        }
        cloud_embed_defaults = {
            "text-embedding-3-small",
            "text-embedding-004",
            DEFAULT_CLOUD_EMBED_MODEL,
        }

        if self.uses_local_chat():
            updates["base_url"] = self.ollama_base_url.rstrip("/")
            if not self.llm_base_url:
                updates["llm_base_url"] = self.ollama_base_url.rstrip("/")
            if self.llm_model in cloud_llm_defaults or not self.llm_model:
                updates["llm_model"] = self.local_llm_model
            if not self._shared_api_key() and not self.api_key and not self.llm_api_key:
                updates["api_key"] = "ollama"

        if self.uses_local_embed():
            if not self.embed_base_url or self._url_looks_cloud(self.embed_base_url):
                updates["embed_base_url"] = self.ollama_base_url.rstrip("/")
            target_embed = (
                self.local_embed_gpu_model if self.uses_gpu_embed() else self.local_embed_model
            )
            if (
                self.embed_model in cloud_embed_defaults
                or not self.embed_model
                or self.embed_model.startswith("text-embedding-")
            ):
                updates["embed_model"] = target_embed
            if not self.embed_api_key or self._url_looks_cloud(self.embed_base_url):
                updates["embed_api_key"] = "ollama"
            if self.embed_batch_size is None:
                updates["embed_batch_size"] = 32
            if self.embed_concurrency is None:
                updates["embed_concurrency"] = 1
            if self.embed_http_timeout is None:
                updates["embed_http_timeout"] = 300.0
        elif self.resolved_embed_backend() == "http" and self.embed_concurrency is None:
            updates["embed_concurrency"] = DEFAULT_HTTP_EMBED_CONCURRENCY

        if (
            self.resolved_chat_axis() == "http"
            and not self.uses_local_chat()
            and (self.llm_model in {"gpt-5.6-terra", "gpt-4o-mini", "gpt-4o"} or not self.llm_model)
        ):
            updates["llm_model"] = DEFAULT_CLOUD_LLM_MODEL

        if updates:
            return self.model_copy(update=updates)
        return self

    def resolved_describe_mode(self) -> str:
        """Return raw | describe."""
        if self.skip_describe or self.skip_llm:
            return "raw"
        mode = (self.describe_mode or "raw").strip().lower()
        if mode == "describe":
            return "describe"
        return "raw"

    def runtime_summary(self) -> dict[str, str | bool]:
        cfg = self.apply_backend_defaults()
        transport = cfg.resolved_embed_backend()
        return {
            "chat_backend": cfg.resolved_chat_backend(),
            "embed_backend": cfg.resolved_embed_axis(),
            "embed_transport": transport,
            "embed_device": cfg.resolved_embed_device(),
            "local_chat": cfg.uses_local_chat(),
            "ollama_required": cfg.ollama_required(),
            "llm_model": cfg.llm_model,
            "embed_model": cfg.embed_model,
            "llm_base_url": cfg.resolve_llm_base_url() if cfg.resolved_chat_backend() == "http" else "",
            "embed_base_url": cfg.resolve_embed_base_url() if transport == "http" else "",
            "profile": cfg.resolved_profile(),
            "describe_mode": cfg.resolved_describe_mode(),
            "parallel_llm": cfg.resolved_chat_backend() == "http",
            "embed_batch_size": int(getattr(cfg, "embed_batch_size", 0) or 0),
            "embed_concurrency": int(getattr(cfg, "embed_concurrency", 0) or 0),
            "effective_embed_parallel": int(getattr(cfg, "embed_concurrency", 0) or 0) > 1,
        }

    def resolved_focus(self) -> str:
        mode = (self.focus or "balanced").strip().lower().replace("-", "_")
        if mode in {"smell", "smells"}:
            return "smells"
        if mode in {"clone", "clones"}:
            return "clones"
        if mode in {"slop", "ai_slop", "ai-slop"}:
            return "slop"
        if mode in {"cross_repo", "crossrepo", "cross"}:
            return "cross_repo"
        if mode in {"strict", "high_effort", "high-effort"}:
            return "strict"
        return "balanced"

    def model_post_init(self, __context: object) -> None:
        provider = (self.provider or "openai").lower()
        if provider in {"openai", "oai"}:
            if not self.base_url:
                self.base_url = OPENAI_BASE_URL
            if self.llm_model in {
                "gpt-4o-mini",
                "gpt-4o",
                "gpt-5.6-terra",
                "gemini-2.0-flash",
                "gemini-2.5-flash",
                "gemini-flash-latest",
                "gemini-flash-lite-latest",
            }:
                self.llm_model = DEFAULT_CLOUD_LLM_MODEL
            if self.embed_model in {
                "text-embedding-004",
                "gemini-embedding-001",
            }:
                self.embed_model = DEFAULT_CLOUD_EMBED_MODEL

    def resolve_test_cmd(self) -> str:
        if self.test_cmd:
            return self.test_cmd
        if self.validate_cmd:
            return self.validate_cmd
        from autorefactor.testing.toolchains import detect_workspace_test_command

        try:
            repos = self.discover_repos()
        except Exception:  # noqa: BLE001
            return "pytest -q"
        return detect_workspace_test_command(repos, self)

    def _shared_api_key(self) -> str | None:
        return (
            self.api_key
            or os.environ.get("AUTOREFACTOR_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
        )

    def resolve_api_key(self) -> str:
        key = self._shared_api_key()
        if not key:
            raise RuntimeError(MISSING_API_KEY_HINT)
        return key

    def resolve_llm_api_key(self) -> str:
        if self.uses_local_chat():
            return self.llm_api_key or self._shared_api_key() or "ollama"
        key = self.llm_api_key or self._shared_api_key() or os.environ.get("LLM_API_KEY")
        if not key:
            raise RuntimeError(MISSING_API_KEY_HINT)
        return key

    def resolve_embed_api_key(self) -> str:
        if self.uses_local_embed():
            return self.embed_api_key or self._shared_api_key() or "ollama"
        key = self.embed_api_key or self._shared_api_key()
        if not key:
            raise RuntimeError(MISSING_API_KEY_HINT)
        return key

    def resolve_agent_api_key(self) -> str:
        key = (
            self.agent_api_key
            or os.environ.get("AUTOREFACTOR_AGENT_API_KEY")
            or os.environ.get("LLM_API_KEY")
            or self._shared_api_key()
        )
        if not key:
            raise RuntimeError(
                "Missing agent API key. Set AUTOREFACTOR_AGENT_API_KEY or AUTOREFACTOR_API_KEY."
            )
        return key

    def resolve_llm_base_url(self) -> str:
        if self.uses_local_chat():
            url = (self.llm_base_url or self.base_url or self.ollama_base_url).rstrip("/")
            if self._url_looks_cloud(url):
                return self.ollama_base_url.rstrip("/")
            return url
        return (self.llm_base_url or self.base_url).rstrip("/")

    def resolve_embed_base_url(self) -> str:
        if self.uses_local_embed():
            url = (self.embed_base_url or self.ollama_base_url).rstrip("/")
            if self._url_looks_cloud(url):
                return self.ollama_base_url.rstrip("/")
            return url
        return (self.embed_base_url or self.base_url).rstrip("/")

    def resolve_agent_base_url(self) -> str | None:
        # OpenHands Cloud often needs no custom base_url; allow empty
        raw = self.agent_base_url
        if raw is None:
            # Only fall back to pipeline base_url when agent key is the shared key
            if self.agent_api_key or os.environ.get("AUTOREFACTOR_AGENT_API_KEY"):
                return None
            return self.base_url.rstrip("/")
        raw = raw.strip()
        return raw.rstrip("/") if raw else None

    def resolve_agent_model(self) -> str:
        return (
            self.agent_model
            or os.environ.get("LLM_MODEL")
            or self.llm_model
        )

    def resolve_cache_dir(self) -> Path:
        if self.cache_dir:
            path = self.cache_dir
        else:
            path = self.out_dir / "cache"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def resolve_out_dir(self) -> Path:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        return self.out_dir

    def discover_repos(self) -> list[Path]:
        root = self.workspace.resolve()
        if not root.exists():
            raise RuntimeError(f"Workspace does not exist: {root}")
        excluded = self.excluded_repo_names()

        def keep(path: Path) -> bool:
            return path.name not in excluded

        if (root / ".git").exists() or any(
            (root / marker).exists()
            for marker in ("pyproject.toml", "pom.xml", "go.mod", "package.json")
        ):
            children = [
                p
                for p in sorted(root.iterdir())
                if p.is_dir() and p.name not in IGNORE_DIR_NAMES and not p.name.startswith(".")
            ]
            repo_children = [
                p
                for p in children
                if keep(p)
                and (
                    (p / ".git").exists()
                    or any(
                        (p / marker).exists()
                        for marker in (
                            "pyproject.toml",
                            "pom.xml",
                            "go.mod",
                            "package.json",
                            "Cargo.toml",
                        )
                    )
                )
            ]
            if repo_children:
                return repo_children
            return [root] if keep(root) else []

        repos = [
            p
            for p in sorted(root.iterdir())
            if p.is_dir()
            and p.name not in IGNORE_DIR_NAMES
            and not p.name.startswith(".")
            and keep(p)
        ]
        if not repos:
            return [root] if keep(root) else []
        return repos
