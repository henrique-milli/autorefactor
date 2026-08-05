from __future__ import annotations

import logging

from coderazor.models import CodeUnit, UnitKind
from coderazor.providers.base import LLMProvider

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


def embed_units(units: list[CodeUnit], provider: LLMProvider) -> list[CodeUnit]:
    targets = [u for u in units if u.kind in EMBED_KINDS]
    texts = [
        (u.description or u.source_snippet)[:2000]
        for u in targets
    ]
    if not texts:
        return units
    logger.info("Embedding %s units", len(texts))
    vectors = provider.embed_texts(texts)
    if len(vectors) != len(targets):
        raise RuntimeError(
            f"Embedding count mismatch: got {len(vectors)} for {len(targets)} units"
        )
    by_id = {u.id: u for u in units}
    for unit, vector in zip(targets, vectors, strict=True):
        unit.embedding = vector
        by_id[unit.id] = unit
    return list(by_id.values())
