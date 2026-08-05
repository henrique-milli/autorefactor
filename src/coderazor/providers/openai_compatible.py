from __future__ import annotations

import json
import logging
import socket
import time
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from coderazor.providers.base import LLMProvider
from coderazor.retry import backoff_seconds, is_retryable_http

logger = logging.getLogger(__name__)


class OpenAICompatibleProvider(LLMProvider):
    """ChatGPT-style OpenAI-compatible chat + embeddings client.

    Chat and embeddings may share credentials or use separate base_url/api_key/model
    (e.g. Gemini for both, or Gemini embeddings + another chat endpoint).
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
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.llm_model = llm_model
        self.embed_model = embed_model
        self.max_retries = max(0, max_retries)
        self.embed_base_url = (embed_base_url or base_url).rstrip("/")
        self.embed_api_key = embed_api_key or api_key

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
    ) -> dict:
        root = (base_url or self.base_url).rstrip("/")
        key = api_key or self.api_key
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
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
                with urlrequest.urlopen(req, timeout=180) as response:
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
                        "API HTTP 401 Unauthorized. Check CODERAZOR_*_API_KEY credentials. "
                        f"Detail: {detail[:300]}"
                    ) from exc
                raise RuntimeError(f"API HTTP {exc.code}: {detail[:500]}") from exc
            except (URLError, socket.timeout, TimeoutError) as exc:
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
            for token in ("gpt-5", "o1", "o3", "o4", "luna")
        )

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        batch_size = 64
        vectors: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            chunk = texts[i : i + batch_size]
            data = self._request_json(
                "POST",
                "embeddings",
                {"model": self.embed_model, "input": chunk},
                base_url=self.embed_base_url,
                api_key=self.embed_api_key,
            )
            items = data.get("data")
            if not isinstance(items, list):
                raise RuntimeError(f"Unexpected embeddings response: {data!r}"[:400])
            ordered = sorted(items, key=lambda row: row.get("index", 0))
            vectors.extend([list(row["embedding"]) for row in ordered])
        return vectors


def build_openai_compatible_from_config(config: object) -> OpenAICompatibleProvider:
    from coderazor.config import CoderazorConfig

    if not isinstance(config, CoderazorConfig):
        raise TypeError("expected CoderazorConfig")
    return OpenAICompatibleProvider(
        base_url=config.resolve_llm_base_url(),
        api_key=config.resolve_llm_api_key(),
        llm_model=config.llm_model,
        embed_model=config.embed_model,
        max_retries=config.max_retries,
        embed_base_url=config.resolve_embed_base_url(),
        embed_api_key=config.resolve_embed_api_key(),
    )

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
        if '"description"' in system or "behavioral summary" in system:
            name_line = next(
                (line for line in user.splitlines() if line.startswith("qualified_name:")),
                "qualified_name: unit",
            )
            name = name_line.split(":", 1)[-1].strip()
            return {
                "description": f"Stub summary for {name}: handles core logic described in the snippet.",
                "tags": ["stub", "auto"],
            }
        if "reconcile" in system.lower() or "decisions" in system.lower():
            # Emit one decision per flag line when possible
            flag_ids = []
            for line in user.splitlines():
                if "id=" in line:
                    part = line.split("id=", 1)[1].split()[0]
                    flag_ids.append(part)
            decisions = []
            for fid in flag_ids or ["stub"]:
                action = "extract_shared"
                if "action=merge" in user:
                    action = "merge"
                elif "action=split" in user:
                    action = "split"
                decisions.append(
                    {
                        "title": f"Stub decision for {fid[:8]}",
                        "action": action,
                        "rationale": "Stub reconcile accepts the component.",
                        "confidence": 0.72,
                        "flag_ids": [fid] if fid != "stub" else [],
                        "unit_ids": [],
                        "valid": True,
                    }
                )
            return {"decisions": decisions}
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
