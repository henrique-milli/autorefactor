from __future__ import annotations

from pathlib import Path

from autorefactor.domain import (
    AnalysisGoal,
    CodeSpan,
    CodeUnit,
    UnitKind,
    content_hash,
)
from autorefactor.enrich.embed import compose_embed_text, embedding_cache_key, embed_units
from autorefactor.enrich.facets import attach_text_facets
from autorefactor.index.store import IndexStore
from autorefactor.providers import StubProvider
from autorefactor.sensors.similarity import emit_similarity_findings


def _fn(
    *,
    uid: str = "u1",
    repo: str = "demo",
    path: str = "mod.py",
    qn: str = "mod.add",
    snippet: str,
    start_byte: int = 0,
    documentation: str | None = None,
    body: str | None = None,
    signature: str | None = None,
) -> CodeUnit:
    return CodeUnit(
        id=uid,
        repo=repo,
        path=path,
        language="python",
        kind=UnitKind.FUNCTION,
        qualified_name=qn,
        source_snippet=snippet,
        content_hash=content_hash(snippet),
        span=CodeSpan(start_line=1, end_line=snippet.count("\n") + 1, start_byte=start_byte),
        documentation=documentation,
        body=body,
        signature=signature,
    )


def test_attach_facets_extracts_python_docstring(tmp_path: Path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    src = '''\
def add(a, b):
    """Sum two numbers for billing."""
    return a + b
'''
    (repo / "mod.py").write_text(src, encoding="utf-8")
    snippet = src[src.index("def ") :]
    unit = _fn(snippet=snippet, start_byte=src.index("def "))
    out = attach_text_facets(tmp_path, [unit])
    assert out[0].documentation
    assert "billing" in (out[0].documentation or "").lower()
    assert out[0].body
    assert out[0].signature and out[0].signature.startswith("def add")


def test_attach_facets_java_leading_javadoc(tmp_path: Path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    src = """\
public class A {
  /**
   * Handles PAM client failures.
   */
  public void handle() {
    return;
  }
}
"""
    (repo / "A.java").write_text(src, encoding="utf-8")
    start = src.index("public void handle")
    snippet = src[start : src.index("}", start) + 1]
    unit = CodeUnit(
        id="j1",
        repo="demo",
        path="A.java",
        language="java",
        kind=UnitKind.FUNCTION,
        qualified_name="A.handle",
        source_snippet=snippet,
        content_hash=content_hash(snippet),
        span=CodeSpan(start_line=5, end_line=7, start_byte=start),
    )
    out = attach_text_facets(tmp_path, [unit])
    assert out[0].documentation
    assert "PAM" in (out[0].documentation or "")
    full = compose_embed_text(out[0], "full")
    assert "PAM" in full
    assert "handle" in full


def test_compose_views_and_cache_keys_differ() -> None:
    u = _fn(
        snippet="def f():\n    return 1\n",
        documentation="Does a thing",
        body="def f():\n    return 1\n",
        signature="def f():",
    )
    code = compose_embed_text(u, "code")
    doc = compose_embed_text(u, "doc")
    full = compose_embed_text(u, "full")
    assert "Does a thing" in doc
    assert "Does a thing" in full
    assert "return" in code
    assert embedding_cache_key(u, embed_model="m", view="code") != embedding_cache_key(
        u, embed_model="m", view="doc"
    )
    assert embedding_cache_key(u, embed_model="m", view="full") != embedding_cache_key(
        u, embed_model="m", view="code"
    )


def test_embed_units_fills_primary_and_secondary(tmp_path: Path) -> None:
    store = IndexStore(tmp_path / "idx.db")
    u = _fn(
        snippet="def f():\n    return 1\n",
        documentation="Important behavior note",
        body="def f():\n    return 1\n",
        signature="def f():",
    )
    out = embed_units([u], StubProvider(), store, embed_model="stub")
    assert out[0].embedding
    assert "code" in out[0].embeddings
    assert "doc" in out[0].embeddings

    class Boom(StubProvider):
        def embed_texts(self, texts, **kwargs):  # noqa: ANN001
            raise AssertionError("expected cache hit")

    again = embed_units(
        [u.model_copy(update={"embedding": None, "embeddings": {}})],
        Boom(),
        store,
        embed_model="stub",
    )
    assert again[0].embedding == out[0].embedding
    store.close()


def test_similarity_records_sim_code_metric() -> None:
    vec = [1.0, 0.0, 0.0]
    code = [0.9, 0.1, 0.0]
    snippet = (
        "def process_bonus(x, y, z):\n"
        "    # nontrivial body so low-signal filter does not skip\n"
        "    total = x + y + z\n"
        "    if total < 0:\n"
        "        return 0\n"
        "    return total * 2\n"
    )
    a = _fn(uid="a", qn="alpha.process_bonus", path="a.py", snippet=snippet)
    b = _fn(uid="b", qn="beta.process_bonus", path="b.py", snippet=snippet)
    a = a.model_copy(update={"embedding": vec, "embeddings": {"code": code}})
    b = b.model_copy(update={"embedding": vec, "embeddings": {"code": code}})
    findings = emit_similarity_findings(
        [a, b],
        kinds={UnitKind.FUNCTION},
        goal=AnalysisGoal.METHOD_SIMILARITY,
        sensor="sim.method",
        threshold=0.5,
        max_pairs=5,
    )
    assert findings
    assert "sim_code" in findings[0].metrics
    assert "sim_full" in findings[0].metrics
    assert findings[0].metrics["sim_code"] > 0.5
