from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field
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

# Gemini OpenAI-compatible gateway (chat + embeddings)
GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"


class CoderazorConfig(BaseSettings):
    """Provider credentials are layered:

    1. Role-specific: ``llm_*``, ``embed_*``, ``agent_*``
    2. Shared fallback: ``api_key`` / ``base_url`` / models
    3. Well-known env aliases: ``OPENAI_API_KEY``, ``GOOGLE_API_KEY``, ``GEMINI_API_KEY``, ``LLM_API_KEY``

    Use one shared key for chat+embed, or split llm_* / embed_* credentials.
    """

    model_config = SettingsConfigDict(
        env_prefix="CODERAZOR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    workspace: Path = Field(default_factory=lambda: Path.cwd())
    out_dir: Path = Field(default_factory=lambda: Path("out"))

    # Shared defaults (single-provider mode)
    base_url: str = "https://api.openai.com/v1"
    api_key: str | None = None
    llm_model: str = "gpt-4o-mini"
    embed_model: str = "text-embedding-3-small"

    # Optional per-role overrides
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    embed_api_key: str | None = None
    embed_base_url: str | None = None
    agent_api_key: str | None = None
    agent_base_url: str | None = None
    agent_model: str | None = None

    # Convenience: set provider=gemini to default Gemini OpenAI-compat endpoints/models
    provider: str | None = None

    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD
    describe_concurrency: int = 16
    max_retries: int = 10
    validate_cmd: str | None = None
    test_cmd: str | None = None
    require_tests: bool = True
    require_harness: bool = True
    cache_dir: Path | None = None
    repo_globs: list[str] = Field(default_factory=lambda: ["*"])
    exclude_repos: str = ""  # comma-separated directory names to skip
    long_function_lines: int = 80
    large_file_lines: int = 500
    skip_llm: bool = False
    skip_describe: bool = False
    # describe_mode: embed_only | index | write
    describe_mode: str = "index"
    confidence_threshold: float = 0.6
    # Comma-separated analyzer plugin names
    analyzers: str = "builtin,semgrep,ruff,eslint,java_static,coverage"
    semgrep_config: str | None = None
    coverage_threshold: float = 80.0
    document_languages: str = "java,typescript,javascript,python"
    max_test_iterations: int = 2
    config_file: Path | None = None

    def excluded_repo_names(self) -> set[str]:
        return {p.strip() for p in self.exclude_repos.split(",") if p.strip()}

    def enabled_analyzers(self) -> list[str]:
        return [p.strip() for p in self.analyzers.split(",") if p.strip()]

    def document_language_set(self) -> set[str]:
        return {p.strip().lower() for p in self.document_languages.split(",") if p.strip()}

    def resolved_describe_mode(self) -> str:
        if self.skip_describe or self.skip_llm:
            return "embed_only"
        mode = (self.describe_mode or "index").strip().lower()
        if mode in {"off", "embed", "embed-only", "embed_only"}:
            return "embed_only"
        if mode in {"write", "docs", "document"}:
            return "write"
        return "index"

    def model_post_init(self, __context: object) -> None:
        if (self.provider or "").lower() in {"gemini", "google"}:
            if self.base_url == "https://api.openai.com/v1":
                self.base_url = GEMINI_OPENAI_BASE_URL
            if self.llm_model in {
                "gpt-4o-mini",
                "gemini-2.0-flash",
                "gemini-2.5-flash",
                "gemini-flash-latest",
            }:
                self.llm_model = "gemini-flash-lite-latest"
            if self.embed_model in {"text-embedding-3-small", "text-embedding-004"}:
                self.embed_model = "gemini-embedding-001"

    def resolve_test_cmd(self) -> str:
        if self.test_cmd:
            return self.test_cmd
        if self.validate_cmd:
            return self.validate_cmd
        from coderazor.testing.toolchains import detect_workspace_test_command

        try:
            repos = self.discover_repos()
        except Exception:  # noqa: BLE001
            return "pytest -q"
        return detect_workspace_test_command(repos, self)

    def _shared_api_key(self) -> str | None:
        return (
            self.api_key
            or os.environ.get("CODERAZOR_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
        )

    def resolve_api_key(self) -> str:
        key = self._shared_api_key()
        if not key:
            raise RuntimeError(
                "Missing API key. Set CODERAZOR_API_KEY (or GOOGLE_API_KEY / OPENAI_API_KEY)."
            )
        return key

    def resolve_llm_api_key(self) -> str:
        key = self.llm_api_key or self._shared_api_key() or os.environ.get("LLM_API_KEY")
        if not key:
            raise RuntimeError(
                "Missing LLM API key. Set CODERAZOR_LLM_API_KEY or CODERAZOR_API_KEY."
            )
        return key

    def resolve_embed_api_key(self) -> str:
        key = self.embed_api_key or self._shared_api_key()
        if not key:
            raise RuntimeError(
                "Missing embedding API key. Set CODERAZOR_EMBED_API_KEY or CODERAZOR_API_KEY."
            )
        return key

    def resolve_agent_api_key(self) -> str:
        key = (
            self.agent_api_key
            or os.environ.get("CODERAZOR_AGENT_API_KEY")
            or os.environ.get("LLM_API_KEY")
            or self._shared_api_key()
        )
        if not key:
            raise RuntimeError(
                "Missing agent API key. Set CODERAZOR_AGENT_API_KEY or CODERAZOR_API_KEY."
            )
        return key

    def resolve_llm_base_url(self) -> str:
        return (self.llm_base_url or self.base_url).rstrip("/")

    def resolve_embed_base_url(self) -> str:
        return (self.embed_base_url or self.base_url).rstrip("/")

    def resolve_agent_base_url(self) -> str | None:
        # OpenHands Cloud often needs no custom base_url; allow empty
        raw = self.agent_base_url
        if raw is None:
            # Only fall back to pipeline base_url when agent key is the shared key
            if self.agent_api_key or os.environ.get("CODERAZOR_AGENT_API_KEY"):
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
