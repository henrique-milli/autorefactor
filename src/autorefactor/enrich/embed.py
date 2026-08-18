from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from autorefactor.domain import CodeUnit, UnitKind, content_hash
from autorefactor.providers.base import LLMProvider

if TYPE_CHECKING:
    from autorefactor.index.store import IndexStore

logger = logging.getLogger(__name__)

EMBED_KINDS = {
    UnitKind.FUNCTION,
    UnitKind.TYPE,
    UnitKind.FILE,
    UnitKind.MODULE,
    UnitKind.PROTO_MESSAGE,
    UnitKind.PROTO_RPC,
    UnitKind.PROTO_SERVICE,
    UnitKind.REPO,
}

PRIMARY_VIEW = "full"
SECONDARY_VIEWS = ("code", "doc")
ALL_VIEWS = (PRIMARY_VIEW, *SECONDARY_VIEWS)


def _doc_channel(unit: CodeUnit) -> str:
    """LLM description preferred; else extracted source documentation."""
    desc = (unit.description or "").strip()
    if desc:
        return desc
    return (unit.documentation or "").strip()


def _code_channel(unit: CodeUnit) -> str:
    sig = (unit.signature or "").strip()
    body = (unit.body or "").strip()
    if sig or body:
        if sig and body:
            if body.startswith(sig):
                return body
            return f"{sig}\n{body}"
        return sig or body
    return (unit.source_snippet or "").strip()


def compose_embed_text(unit: CodeUnit, view: str, *, max_chars: int = 2000) -> str:
    """Compose text for an embed view: code | doc | full."""
    view_l = (view or PRIMARY_VIEW).strip().lower()
    if view_l == "code":
        text = _code_channel(unit)
    elif view_l == "doc":
        text = _doc_channel(unit)
    else:
        # full = doc + code (raw mode includes existing docs)
        doc = _doc_channel(unit)
        code = _code_channel(unit)
        if doc and code:
            text = f"{doc}\n\n{code}"
        else:
            text = doc or code
    return text[:max_chars]


def embedding_cache_key(
    unit: CodeUnit,
    *,
    embed_model: str,
    view: str = PRIMARY_VIEW,
    describe_mode: str | None = None,  # noqa: ARG001 — kept for call-site compat
) -> str:
    """Invalidate when source facets, description, view, or model change."""
    text = compose_embed_text(unit, view)
    text_hash = content_hash(text or "-")
    return f"{unit.content_hash}:{view}:{text_hash}:{embed_model}"


# Back-compat alias used by older tests
def _embed_text(unit: CodeUnit, *, describe_mode: str) -> str:
    """Primary embed text (full view). describe_mode only affects doc channel via description."""
    _ = describe_mode
    return compose_embed_text(unit, PRIMARY_VIEW)


def embed_units(
    units: list[CodeUnit],
    provider: LLMProvider,
    store: IndexStore | None = None,
    *,
    embed_model: str | None = None,
    describe_mode: str = "raw",
    batch_size: int | None = None,
    concurrency: int = 1,
    stats: dict[str, int] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[CodeUnit]:
    """Embed code/doc/full views; primary ``unit.embedding`` is full."""
    targets = [u for u in units if u.kind in EMBED_KINDS]
    if not targets:
        return units

    model = embed_model or getattr(provider, "embed_model", None) or "default"
    mode = (describe_mode or "raw").strip().lower()
    if mode in {"embed_only", "index"}:
        mode = "raw" if mode == "embed_only" else "describe"
    _ = mode  # description already on unit when describe ran

    chunk_size = batch_size
    if chunk_size is None:
        chunk_size = getattr(provider, "embed_batch_size", None)
    if chunk_size is None:
        inner = getattr(provider, "_embed", None)
        if inner is not None:
            chunk_size = getattr(inner, "embed_batch_size", None)
    if chunk_size is None:
        embed_url = (
            getattr(provider, "embed_base_url", None)
            or getattr(getattr(provider, "_embed", None), "embed_base_url", None)
            or getattr(provider, "base_url", None)
        )
        from autorefactor.providers.openai_compatible import resolve_embed_batch_size

        chunk_size = resolve_embed_batch_size(embed_url)
    chunk_size = max(1, int(chunk_size))
    concurrency = max(1, int(concurrency or 1))
    stats_out = stats if stats is not None else {}
    stats_out.setdefault("embed_cache_hits", 0)
    stats_out.setdefault("embed_cache_misses", 0)
    stats_out.setdefault("embed_jobs_total", 0)
    stats_out.setdefault("embed_batches_sent", 0)
    stats_out.setdefault("embed_concurrency", concurrency)
    stats_out.setdefault("embed_batch_size", chunk_size)

    by_id = {u.id: u for u in units}
    # Collect (unit_id, view, text) needing embed
    jobs: list[tuple[str, str, str]] = []
    for unit in targets:
        views_hit: dict[str, list[float]] = dict(unit.embeddings or {})
        primary = unit.embedding
        for view in ALL_VIEWS:
            text = compose_embed_text(unit, view)
            if view != PRIMARY_VIEW and not text.strip():
                continue
            if not text.strip() and view == PRIMARY_VIEW:
                text = (unit.source_snippet or "")[:2000]
            key = embedding_cache_key(unit, embed_model=model, view=view)
            cached = store.get_embedding(key, model) if store is not None else None
            if cached is not None:
                stats_out["embed_cache_hits"] += 1
                if view == PRIMARY_VIEW:
                    primary = cached
                else:
                    views_hit[view] = cached
                continue
            stats_out["embed_cache_misses"] += 1
            jobs.append((unit.id, view, text or " "))

        by_id[unit.id] = unit.model_copy(
            update={"embedding": primary, "embeddings": views_hit}
        )

    if jobs:
        stats_out["embed_jobs_total"] = len(jobs)
        logger.info(
            "Embedding %s view-texts across %s units (batch_size=%s concurrency=%s)",
            len(jobs),
            len(targets),
            chunk_size,
            concurrency,
        )
        chunks = [jobs[start : start + chunk_size] for start in range(0, len(jobs), chunk_size)]
        stats_out["embed_batches_sent"] = len(chunks)

        def _embed_chunk(chunk: list[tuple[str, str, str]]) -> list[list[float]]:
            texts = [t for _, _, t in chunk]
            vectors = provider.embed_texts(texts)
            if len(vectors) != len(chunk):
                raise RuntimeError(
                    f"Embedding count mismatch: got {len(vectors)} for {len(chunk)} texts"
                )
            return vectors

        def _apply_chunk(
            chunk: list[tuple[str, str, str]],
            vectors: list[list[float]],
            *,
            done_before: int,
        ) -> int:
            for (uid, view, _text), vector in zip(chunk, vectors, strict=True):
                unit = by_id[uid]
                emb_map = dict(unit.embeddings or {})
                if view == PRIMARY_VIEW:
                    unit = unit.model_copy(update={"embedding": vector, "embeddings": emb_map})
                else:
                    emb_map[view] = vector
                    unit = unit.model_copy(update={"embeddings": emb_map})
                by_id[uid] = unit
                if store is not None:
                    key = embedding_cache_key(unit, embed_model=model, view=view)
                    store.put_embedding(key, model, vector)
            done = done_before + len(chunk)
            if on_progress is not None:
                on_progress(done, len(jobs))
            return done

        done = 0
        if concurrency > 1 and len(chunks) > 1:
            with ThreadPoolExecutor(max_workers=min(concurrency, len(chunks))) as pool:
                futures = {
                    pool.submit(_embed_chunk, chunk): (idx, chunk)
                    for idx, chunk in enumerate(chunks)
                }
                for fut in futures:
                    idx, chunk = futures[fut]
                    vectors = fut.result()
                    done = _apply_chunk(chunk, vectors, done_before=done)
        else:
            for chunk in chunks:
                vectors = _embed_chunk(chunk)
                done = _apply_chunk(chunk, vectors, done_before=done)
    else:
        logger.info("Embedding fully cached for %s units", len(targets))

    return list(by_id.values())
