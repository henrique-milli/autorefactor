from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from autorefactor.config import (
    DEFAULT_HTTP_EMBED_CONCURRENCY,
    AutorefactorConfig,
)
from autorefactor.domain import CodeSpan, CodeUnit, UnitKind, content_hash
from autorefactor.enrich.describe import describe_units
from autorefactor.enrich.embed import embed_units
from autorefactor.freshness import _run_has_analyze_artifacts
from autorefactor.index.store import IndexStore
from autorefactor.mcp import tools as T
from autorefactor.providers.base import LLMProvider
from autorefactor.models import DescriptionResult
from autorefactor.runtime_status import runtime_health_payload


def _cfg(**kwargs: Any) -> AutorefactorConfig:
    kwargs.setdefault("api_key", None)
    return AutorefactorConfig(_env_file=None, **kwargs)


def _unit(i: int, *, body: str | None = None) -> CodeUnit:
    src = body or f"def f{i}():\n    return {i}\n"
    return CodeUnit(
        id=f"u{i}",
        repo="r",
        path=f"A{i}.py",
        qualified_name=f"A{i}.f",
        kind=UnitKind.FUNCTION,
        language="python",
        body=src,
        signature=f"def f{i}():",
        source_snippet=src,
        content_hash=content_hash(src),
        span=CodeSpan(start_line=1, end_line=2),
    )


def test_http_embed_concurrency_defaults_parallel() -> None:
    cfg = _cfg(chat_backend="http", embed_backend="http", api_key="sk-x").apply_backend_defaults()
    assert cfg.embed_concurrency == DEFAULT_HTTP_EMBED_CONCURRENCY
    runtime = cfg.runtime_summary()
    assert runtime["embed_concurrency"] == DEFAULT_HTTP_EMBED_CONCURRENCY
    assert runtime["effective_embed_parallel"] is True


def test_local_embed_concurrency_stays_sequential() -> None:
    cfg = _cfg(chat_backend="agent", embed_backend="local").apply_backend_defaults()
    assert cfg.embed_concurrency == 1
    runtime = cfg.runtime_summary()
    assert runtime["embed_concurrency"] == 1
    assert runtime["effective_embed_parallel"] is False


def test_describe_units_reports_cache_and_seed_stats(tmp_path: Path) -> None:
    class CountingProvider(LLMProvider):
        calls = 0

        def chat_json(self, system: str, user: str, *, temperature: float = 0.2) -> dict:
            return {}

        def embed_texts(self, texts: list[str]) -> list[list[float]]:
            return [[0.0] * 3 for _ in texts]

        def describe_unit(self, **kwargs: Any) -> DescriptionResult:  # type: ignore[no-untyped-def]
            type(self).calls += 1
            return DescriptionResult(description="LLM summary", tags=["llm"])

    seeded = _unit(
        1,
        body=(
            "def settle():\n"
            '    """Settles a bonus ledger entry."""\n'
            "    save()\n"
        ),
    )
    plain = _unit(2, body="def other():\n    return 1\n")
    store = IndexStore(tmp_path / "index.db")
    stats: dict[str, int] = {}

    first = describe_units([seeded, plain], CountingProvider(), store, concurrency=1, stats=stats)
    assert stats["describe_source_seed_hits"] == 1
    assert stats["describe_llm_calls"] == 1
    assert stats["describe_cache_misses"] == 2
    assert "Settles a bonus" in (first[0].description or "")

    stats2: dict[str, int] = {}
    describe_units([seeded, plain], CountingProvider(), store, concurrency=1, stats=stats2)
    assert stats2["describe_cache_hits"] == 2
    assert stats2["describe_llm_calls"] == 0
    assert CountingProvider.calls == 1
    store.close()


def test_embed_units_parallel_preserves_order_and_stats(tmp_path: Path) -> None:
    store = IndexStore(tmp_path / "idx.db")
    units = [_unit(i) for i in range(6)]
    stats: dict[str, int] = {}

    class ParallelProbe(LLMProvider):
        def chat_json(self, system: str, user: str, *, temperature: float = 0.2) -> dict:
            raise RuntimeError("unused")

        def embed_texts(self, texts: list[str]) -> list[list[float]]:
            out: list[list[float]] = []
            for text in texts:
                match = re.search(r"def f(\d+)", text)
                idx = float(match.group(1)) if match else -1.0
                out.append([idx, 0.0])
            return out

    out = embed_units(
        units,
        ParallelProbe(),
        store,
        embed_model="probe-model",
        batch_size=2,
        concurrency=3,
        stats=stats,
    )
    assert stats["embed_cache_misses"] == 12
    assert stats["embed_jobs_total"] == 12
    assert stats["embed_batches_sent"] == 6
    assert stats["embed_concurrency"] == 3
    assert [u.embedding for u in out] == [[float(i), 0.0] for i in range(6)]

    stats2: dict[str, int] = {}
    reset = [u.model_copy(update={"embedding": None, "embeddings": {}}) for u in units]
    embed_units(
        reset,
        ParallelProbe(),
        store,
        embed_model="probe-model",
        batch_size=2,
        concurrency=3,
        stats=stats2,
    )
    assert stats2["embed_cache_hits"] == 12
    assert stats2["embed_batches_sent"] == 0
    store.close()


def test_run_has_analyze_artifacts_fallback_on_attribute_error(tmp_path: Path) -> None:
    out = tmp_path / "run"
    out.mkdir()
    (out / "manifest.json").write_text("{}", encoding="utf-8")
    with patch(
        "autorefactor.paths.run_has_analyze_artifacts",
        side_effect=AttributeError("missing symbol"),
    ):
        assert _run_has_analyze_artifacts(out) is True


def test_get_status_surfaces_runtime_and_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "main.py").write_text("print(1)\n", encoding="utf-8")
    out = tmp_path / "out"
    report = out / "report"
    report.mkdir(parents=True)
    metrics = {
        "pipeline": {
            "runtime": {"embed_concurrency": 4, "effective_embed_parallel": True},
            "describe": {"describe_cache_hits": 3, "describe_llm_calls": 1},
            "embed": {"embed_cache_hits": 10, "embed_batches_sent": 2},
        }
    }
    (report / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    (out / "queue").mkdir(parents=True, exist_ok=True)
    (out / "queue" / "changesets.json").write_text('{"changesets": []}', encoding="utf-8")

    monkeypatch.setattr(T, "resolve_analyze_workspace", lambda _w=None: ws)
    monkeypatch.setattr(T, "resolve_out_dir", lambda _ws, _o=None: out)
    monkeypatch.setattr(T, "_maybe_proxy", lambda *_a, **_k: None)
    monkeypatch.setattr(
        T,
        "load_merged_config",
        lambda **_k: _cfg(
            workspace=ws,
            out_dir=out,
            chat_backend="http",
            embed_backend="http",
            api_key="sk-x",
        ).apply_backend_defaults(),
    )

    status = T.tool_get_status(workspace=str(ws), out=str(out))
    assert status["embed_concurrency"] == DEFAULT_HTTP_EMBED_CONCURRENCY
    assert status["effective_embed_parallel"] is True
    assert status["pipeline"]["describe"]["describe_cache_hits"] == 3
    assert status["pipeline"]["embed"]["embed_cache_hits"] == 10


def test_health_payload_includes_embed_runtime_fields() -> None:
    payload = runtime_health_payload(
        _cfg(chat_backend="http", embed_backend="http", api_key="sk-x").apply_backend_defaults()
    )
    assert "embed_batch_size" in payload
    assert payload["embed_concurrency"] == DEFAULT_HTTP_EMBED_CONCURRENCY
    assert payload["effective_embed_parallel"] is True
    assert payload["describe_mode"] == "raw"
