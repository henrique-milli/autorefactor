from __future__ import annotations

from pathlib import Path

from autorefactor.domain import CodeSpan, CodeUnit, UnitKind, content_hash
from autorefactor.enrich.embed import embed_units
from autorefactor.index.store import IndexStore
from autorefactor.providers import StubProvider


def test_embed_units_uses_content_hash_cache(tmp_path: Path) -> None:
    store = IndexStore(tmp_path / "idx.db")
    unit = CodeUnit(
        id="u1",
        repo="r",
        path="A.py",
        qualified_name="A.f",
        kind=UnitKind.FUNCTION,
        language="python",
        source_snippet="def f():\n    return 1\n",
        content_hash=content_hash("def f():\n    return 1\n"),
        span=CodeSpan(start_line=1, end_line=2),
    )
    provider = StubProvider()
    first = embed_units([unit], provider, store, embed_model="stub-model")
    assert first[0].embedding
    # Second pass should not need provider (wipe provider to explode if called)
    class Boom(StubProvider):
        def embed_texts(self, texts, **kwargs):  # noqa: ANN001
            raise AssertionError("cache miss — should not embed again")

    unit2 = unit.model_copy(update={"embedding": None})
    second = embed_units([unit2], Boom(), store, embed_model="stub-model")
    assert second[0].embedding == first[0].embedding
    store.close()
