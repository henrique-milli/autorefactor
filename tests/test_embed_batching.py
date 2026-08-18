from __future__ import annotations

from pathlib import Path

import pytest

from autorefactor.domain import CodeSpan, CodeUnit, UnitKind, content_hash
from autorefactor.enrich.embed import embedding_cache_key, embed_units
from autorefactor.index.store import IndexStore
from autorefactor.providers.base import LLMProvider
from autorefactor.providers.openai_compatible import (
    OpenAICompatibleProvider,
    resolve_embed_batch_size,
    resolve_embed_http_timeout,
)


def test_resolve_embed_batch_size_prefers_local_batches_for_ollama() -> None:
    assert resolve_embed_batch_size("http://ollama:11434/v1") == 32
    assert resolve_embed_batch_size("http://127.0.0.1:11434/v1") == 32
    assert resolve_embed_batch_size("https://api.openai.com/v1") == 64
    assert resolve_embed_batch_size("http://ollama:11434/v1", override=16) == 16


def test_resolve_embed_http_timeout_is_longer_for_ollama() -> None:
    assert resolve_embed_http_timeout("http://ollama:11434/v1") >= 300
    assert resolve_embed_http_timeout("https://api.openai.com/v1") == 180


def test_composite_provider_surfaces_embed_batch_size() -> None:
    from autorefactor.providers.openai_compatible import CompositeProvider, NullEmbedder

    embed = OpenAICompatibleProvider(
        base_url="http://ollama:11434/v1",
        api_key="ollama",
        llm_model="unused",
        embed_model="nomic-embed-text",
        embed_base_url="http://ollama:11434/v1",
    )
    composite = CompositeProvider(chat=NullEmbedder(), embed=embed)
    assert composite.embed_batch_size == 32
    assert composite.embed_base_url.endswith("11434/v1")


def test_agent_local_defaults_local_embed_batch_size() -> None:
    from autorefactor.config import AutorefactorConfig

    cfg = AutorefactorConfig(
        _env_file=None, chat_backend="agent", embed_backend="local", api_key=None
    ).apply_backend_defaults()
    assert cfg.embed_model == "nomic-embed-text"
    assert cfg.embed_batch_size == 32
    assert cfg.embed_concurrency == 1
    assert cfg.embed_http_timeout == 300.0


def test_openai_compatible_embed_texts_chunks_to_batch_size() -> None:
    provider = OpenAICompatibleProvider(
        base_url="http://ollama:11434/v1",
        api_key="ollama",
        llm_model="unused",
        embed_model="nomic-embed-text",
        embed_base_url="http://ollama:11434/v1",
        embed_api_key="ollama",
    )
    assert provider.embed_batch_size == 32

    seen: list[int] = []

    def fake_request(method, path, payload=None, **kwargs):  # noqa: ANN001, ARG001
        assert path == "embeddings"
        chunk = payload["input"]
        seen.append(len(chunk))
        return {
            "data": [
                {"index": i, "embedding": [float(i), 0.0]} for i in range(len(chunk))
            ]
        }

    provider._request_json = fake_request  # type: ignore[method-assign]
    vectors = provider.embed_texts([f"t{i}" for i in range(40)])
    assert len(vectors) == 40
    assert seen == [32, 8]


def test_embed_units_caches_completed_batches_before_failure(tmp_path: Path) -> None:
    """If a later embed batch fails, earlier vectors must already be in the cache."""
    store = IndexStore(tmp_path / "idx.db")
    units = [
        CodeUnit(
            id=f"u{i}",
            repo="r",
            path=f"A{i}.py",
            qualified_name=f"A{i}.f",
            kind=UnitKind.FUNCTION,
            language="python",
            body=f"def f{i}():\n    return {i}\n",
            signature=f"def f{i}():",
            source_snippet=f"def f{i}():\n    return {i}\n",
            content_hash=content_hash(f"def f{i}():\n    return {i}\n"),
            span=CodeSpan(start_line=1, end_line=2),
        )
        for i in range(3)
    ]

    class Flaky(LLMProvider):
        def __init__(self) -> None:
            self.calls = 0

        def chat_json(self, system: str, user: str, *, temperature: float = 0.2) -> dict:
            raise RuntimeError("chat unused")

        def embed_texts(self, texts: list[str]) -> list[list[float]]:
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("simulated ollama disconnect")
            return [[float(self.calls), float(len(texts))] for _ in texts]

    with pytest.raises(RuntimeError, match="simulated ollama disconnect"):
        embed_units(
            units,
            Flaky(),
            store,
            embed_model="nomic-embed-text",
            batch_size=1,
        )

    key = embedding_cache_key(units[0], embed_model="nomic-embed-text", view="full")
    cached = store.get_embedding(key, "nomic-embed-text")
    assert cached is not None
    assert cached[0] == 1.0
    store.close()
