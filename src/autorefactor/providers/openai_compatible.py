from __future__ import annotations

import json
import logging
import socket
import time
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from autorefactor.providers.base import LLMProvider
from autorefactor.retry import backoff_seconds, is_retryable_http

logger = logging.getLogger(__name__)

DEFAULT_HTTP_TIMEOUT = 180.0
DEFAULT_EMBED_BATCH_SIZE = 64
# CPU Ollama (nomic-embed-text): near-linear ~1.5–1.6s/item; batch 64 ≈ 100s for
# ~2k-char texts. Prefer 32 for headroom under load while keeping fewer round-trips
# than tiny batches. Override with AUTOREFACTOR_EMBED_BATCH_SIZE.
LOCAL_EMBED_BATCH_SIZE = 32
LOCAL_EMBED_HTTP_TIMEOUT = 300.0


def _looks_like_ollama_url(url: str | None) -> bool:
    if not url:
        return False
    low = url.lower()
    return "11434" in low or "ollama" in low


def resolve_embed_batch_size(embed_base_url: str | None, override: int | None = None) -> int:
    if override is not None and override > 0:
        return int(override)
    if _looks_like_ollama_url(embed_base_url):
        return LOCAL_EMBED_BATCH_SIZE
    return DEFAULT_EMBED_BATCH_SIZE


def resolve_embed_http_timeout(
    embed_base_url: str | None, override: float | None = None
) -> float:
    if override is not None and override > 0:
        return float(override)
    if _looks_like_ollama_url(embed_base_url):
        return LOCAL_EMBED_HTTP_TIMEOUT
    return DEFAULT_HTTP_TIMEOUT


class OpenAICompatibleProvider(LLMProvider):
    """ChatGPT-style OpenAI-compatible chat + embeddings client.

    Chat and embeddings may share credentials or use separate base_url/api_key/model
    (e.g. OpenAI for both, or split llm/embed endpoints).
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        llm_model: str,
        embed_model: str,
        max_retries: int = 4,
        embed_base_url: str | None = None,
        embed_api_key: str | None = None,
        embed_batch_size: int | None = None,
        embed_http_timeout: float | None = None,
        http_timeout: float | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.llm_model = llm_model
        self.embed_model = embed_model
        self.max_retries = max(0, max_retries)
        self.embed_base_url = (embed_base_url or base_url).rstrip("/")
        self.embed_api_key = embed_api_key or api_key
        self.http_timeout = float(http_timeout) if http_timeout and http_timeout > 0 else DEFAULT_HTTP_TIMEOUT
        self.embed_batch_size = resolve_embed_batch_size(
            self.embed_base_url, override=embed_batch_size
        )
        self.embed_http_timeout = resolve_embed_http_timeout(
            self.embed_base_url, override=embed_http_timeout
        )

    def _endpoint(self, base_url: str, path: str) -> str:
        return f"{base_url}/{path.lstrip('/')}"

    def _sleep_before_retry(
        self,
        attempt: int,
        *,
        reason: str,
        status_code: int | None = None,
        retry_after: str | None = None,
    ) -> None:
        delay = backoff_seconds(attempt, status_code=status_code, retry_after=retry_after)
        logger.warning(
            "Provider %s — retry %s/%s in %.1fs",
            reason,
            attempt + 1,
            self.max_retries,
            delay,
        )
        time.sleep(delay)

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
    ) -> dict:
        root = (base_url or self.base_url).rstrip("/")
        key = api_key or self.api_key
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request_timeout = self.http_timeout if timeout is None else float(timeout)
        attempt = 0
        while True:
            req = urlrequest.Request(
                self._endpoint(root, path),
                data=body,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                method=method,
            )
            try:
                with urlrequest.urlopen(req, timeout=request_timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="ignore")
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                if is_retryable_http(exc.code) and attempt < self.max_retries:
                    self._sleep_before_retry(
                        attempt,
                        reason=f"HTTP {exc.code} {path}",
                        status_code=exc.code,
                        retry_after=retry_after,
                    )
                    attempt += 1
                    continue
                if exc.code == 401:
                    raise RuntimeError(
                        "API HTTP 401 Unauthorized. Check AUTOREFACTOR_*_API_KEY credentials. "
                        f"Detail: {detail[:300]}"
                    ) from exc
                raise RuntimeError(f"API HTTP {exc.code}: {detail[:500]}") from exc
            except (URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
                if isinstance(exc, HTTPError):
                    raise
                if attempt < self.max_retries:
                    self._sleep_before_retry(attempt, reason=f"connection error {path}")
                    attempt += 1
                    continue
                raise RuntimeError(f"API connection error: {exc}") from exc

    def chat_json(self, system: str, user: str, *, temperature: float | None = 0.2) -> dict:
        payload: dict = {
            "model": self.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
        }
        # gpt-5 / o-series style models often only allow the default temperature
        if temperature is not None and not self._model_locks_temperature():
            payload["temperature"] = temperature
        for attempt in range(self.max_retries + 1):
            try:
                data = self._request_json("POST", "chat/completions", payload)
                content = data["choices"][0]["message"]["content"] or "{}"
                # Some models wrap JSON in fences
                content = content.strip()
                if content.startswith("```"):
                    content = content.strip("`")
                    if content.startswith("json"):
                        content = content[4:].strip()
                return json.loads(content)
            except RuntimeError as exc:
                message = str(exc)
                if (
                    "temperature" in message.lower()
                    and "unsupported" in message.lower()
                    and "temperature" in payload
                ):
                    payload.pop("temperature", None)
                    continue
                if attempt < self.max_retries and "response_format" in payload:
                    # Retry without response_format for picky gateways
                    if "json" in message.lower() or "response_format" in message.lower():
                        payload.pop("response_format", None)
                        time.sleep(backoff_seconds(attempt))
                        continue
                raise
            except json.JSONDecodeError:
                if attempt < self.max_retries:
                    # Retry without response_format for picky gateways
                    payload.pop("response_format", None)
                    time.sleep(backoff_seconds(attempt))
                    continue
                raise RuntimeError("Provider returned invalid JSON")
        raise RuntimeError("chat_json failed")

    def _model_locks_temperature(self) -> bool:
        name = (self.llm_model or "").lower()
        return any(
            token in name
            for token in ("gpt-5", "o1", "o3", "o4", "terra", "luna")
        )

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        batch_size = max(1, int(self.embed_batch_size))
        vectors: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            chunk = texts[i : i + batch_size]
            data = self._request_json(
                "POST",
                "embeddings",
                {"model": self.embed_model, "input": chunk},
                base_url=self.embed_base_url,
                api_key=self.embed_api_key,
                timeout=self.embed_http_timeout,
            )
            items = data.get("data")
            if not isinstance(items, list):
                raise RuntimeError(f"Unexpected embeddings response: {data!r}"[:400])
            ordered = sorted(items, key=lambda row: row.get("index", 0))
            vectors.extend([list(row["embedding"]) for row in ordered])
        return vectors


def build_openai_compatible_from_config(config: object) -> OpenAICompatibleProvider:
    from autorefactor.config import AutorefactorConfig

    if not isinstance(config, AutorefactorConfig):
        raise TypeError("expected AutorefactorConfig")
    cfg = config.apply_backend_defaults()
    return OpenAICompatibleProvider(
        base_url=cfg.resolve_llm_base_url(),
        api_key=cfg.resolve_llm_api_key(),
        llm_model=cfg.llm_model,
        embed_model=cfg.embed_model,
        max_retries=cfg.max_retries,
        embed_base_url=cfg.resolve_embed_base_url(),
        embed_api_key=cfg.resolve_embed_api_key(),
        embed_batch_size=getattr(cfg, "embed_batch_size", None),
        embed_http_timeout=getattr(cfg, "embed_http_timeout", None),
    )


class DeferredToAgent(RuntimeError):
    """Raised when chat work must be handled by the host IDE agent."""


class AgentDeferredProvider(LLMProvider):
    """Chat is owned by the host agent; embeddings may still be HTTP or null."""

    def __init__(self, embedder: LLMProvider | None = None) -> None:
        self._embedder = embedder or NullEmbedder()

    def chat_json(self, system: str, user: str, *, temperature: float = 0.2) -> dict:
        raise DeferredToAgent(
            "chat_backend=agent — call get_next_enrich_task / submit_enrich_result "
            "(or use StubProvider for offline heuristic planning)."
        )

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return self._embedder.embed_texts(texts)


class NullEmbedder(LLMProvider):
    """No embeddings — embed_texts returns empty vectors (callers should skip embed)."""

    def chat_json(self, system: str, user: str, *, temperature: float = 0.2) -> dict:
        raise RuntimeError("NullEmbedder does not support chat")

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[] for _ in texts]


class CompositeProvider(LLMProvider):
    """Delegate chat and embed to separate backends."""

    def __init__(self, *, chat: LLMProvider, embed: LLMProvider) -> None:
        self._chat = chat
        self._embed = embed
        # Surface embed tuning so callers (embed_units) see Ollama batch/timeout.
        self.embed_model = getattr(embed, "embed_model", None)
        self.embed_base_url = getattr(embed, "embed_base_url", None)
        self.embed_batch_size = getattr(embed, "embed_batch_size", None)
        self.embed_http_timeout = getattr(embed, "embed_http_timeout", None)

    def chat_json(self, system: str, user: str, *, temperature: float = 0.2) -> dict:
        return self._chat.chat_json(system, user, temperature=temperature)

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return self._embed.embed_texts(texts)


def build_embedder(config: object) -> LLMProvider:
    from autorefactor.config import AutorefactorConfig

    if not isinstance(config, AutorefactorConfig):
        raise TypeError("expected AutorefactorConfig")
    cfg = config.apply_backend_defaults()
    backend = cfg.resolved_embed_backend()
    if backend == "none":
        return NullEmbedder()
    if backend == "stub":
        return StubProvider()
    # http — reuse OpenAI-compatible embed path via a chat-stub composite
    http = OpenAICompatibleProvider(
        base_url=cfg.resolve_embed_base_url(),
        api_key=cfg.resolve_embed_api_key(),
        llm_model=cfg.llm_model,
        embed_model=cfg.embed_model,
        max_retries=cfg.max_retries,
        embed_base_url=cfg.resolve_embed_base_url(),
        embed_api_key=cfg.resolve_embed_api_key(),
        embed_batch_size=getattr(cfg, "embed_batch_size", None),
        embed_http_timeout=getattr(cfg, "embed_http_timeout", None),
    )
    return http


def build_provider_from_config(config: object) -> LLMProvider:
    """Build a composite chat+embed provider from config backends."""
    from autorefactor.config import AutorefactorConfig

    if not isinstance(config, AutorefactorConfig):
        raise TypeError("expected AutorefactorConfig")
    cfg = config.apply_backend_defaults()
    if cfg.skip_llm:
        return StubProvider()

    embed = build_embedder(cfg)
    chat_backend = cfg.resolved_chat_backend()
    if chat_backend == "agent":
        # Analyze uses StubProvider for planning; AgentDeferred for explicit deferral.
        return CompositeProvider(chat=StubProvider(), embed=embed)
    if chat_backend == "stub":
        return CompositeProvider(chat=StubProvider(), embed=embed)
    http = build_openai_compatible_from_config(cfg)
    if cfg.resolved_embed_backend() == "http" and (
        cfg.resolve_embed_base_url() != cfg.resolve_llm_base_url()
        or cfg.resolve_embed_api_key() != cfg.resolve_llm_api_key()
    ):
        return CompositeProvider(chat=http, embed=embed)
    return CompositeProvider(chat=http, embed=embed)


class StubProvider(LLMProvider):
    """Deterministic offline provider for dry runs without API keys."""

    def chat_json(self, system: str, user: str, *, temperature: float = 0.2) -> dict:
        # Heuristic: confirm similarity/smell flags unless marked trivial
        if "false positive" in user.lower() or "getter" in user.lower():
            return {
                "valid": False,
                "reasoning": "Looks like a trivial or false-positive signal.",
                "action": "document",
            }
        if (
            '"description"' in system
            or '"doc"' in system
            or "behavioral summary" in system
            or "documentation for a code unit" in system.lower()
            or "docstrings for a" in system.lower()
            or "jsdoc" in system.lower()
            or "go doc comments" in system.lower()
            or "java documentation" in system.lower()
        ):
            name_line = next(
                (line for line in user.splitlines() if line.startswith("qualified_name:")),
                "qualified_name: unit",
            )
            name = name_line.split(":", 1)[-1].strip()
            return {
                "doc": f"Stub summary for {name}: handles core logic described in the snippet.",
                "tags": ["stub", "auto"],
            }
        if (
            "reconcile" in system.lower()
            or "decisions" in system.lower()
            or "plan refactor" in system.lower()
            or "planningbrief" in system.lower()
        ):
            # Emit one decision per flag line; parse unit ids from units=uid|...
            decisions = []
            for line in user.splitlines():
                if "id=" not in line:
                    continue
                fid = line.split("id=", 1)[1].split()[0]
                action = "extract_shared"
                action_token = ""
                for key in ("action_hint=", "action="):
                    if key in line:
                        action_token = line.split(key, 1)[1].split()[0]
                        break
                if action_token in {
                    "split",
                    "delete_duplicate",
                    "merge",
                    "move",
                    "ensure_tests",
                    "document",
                    "extract_shared",
                }:
                    action = action_token
                unit_ids: list[str] = []
                if "units=" in line:
                    raw = line.split("units=", 1)[1].strip()
                    parts = raw.replace("; ", ", ").split(", ")
                    for part in parts:
                        part = part.strip()
                        if "|" in part:
                            unit_ids.append(part.split("|", 1)[0])
                title = f"Offline plan for {fid[:8]}"
                if "title=" in line:
                    rest = line.split("title=", 1)[1]
                    # Fields after title are evidence= / hints= / units=
                    for stop in (" evidence=", " hints=", " units="):
                        if stop in rest:
                            rest = rest.split(stop, 1)[0]
                    title = rest.strip() or title
                decisions.append(
                    {
                        "title": title[:160],
                        "action": action,
                        "rationale": (
                            "Offline stub planner: accepted sensor evidence without LLM "
                            "validation. Re-run without --skip-llm before applying."
                        ),
                        "confidence": 0.62 if action != "delete_duplicate" else 0.7,
                        "finding_ids": [fid],
                        "flag_ids": [fid],
                        "unit_ids": unit_ids,
                        "valid": True,
                    }
                )
            return {
                "decisions": decisions
                or [
                    {
                        "title": "empty",
                        "action": "document",
                        "valid": False,
                        "unit_ids": [],
                        "flag_ids": [],
                        "confidence": 0.1,
                        "rationale": "no flags",
                    }
                ]
            }
        return {
            "valid": True,
            "reasoning": "Stub provider accepts the flag for planning.",
            "action": (
                "extract_shared"
                if "suggested_action: extract_shared" in user
                else "merge"
                if "suggested_action: merge" in user
                else "delete_duplicate"
                if "suggested_action: delete_duplicate" in user
                else "split"
                if "suggested_action: split" in user
                else "move"
                if "suggested_action: move" in user
                else "document"
            ),
        }

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vec = [0.0] * 64
            data = text.encode("utf-8", errors="ignore")
            for i, ch in enumerate(data[:2048]):
                vec[i % 64] += ((ch * (i + 3)) % 97) / 97.0
            # Mix in a stable fingerprint so identical texts still align
            fingerprint = sum(data) % 64
            vec[fingerprint] += 1.5
            norm = sum(v * v for v in vec) ** 0.5 or 1.0
            vectors.append([v / norm for v in vec])
        return vectors
