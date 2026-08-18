from __future__ import annotations

from pathlib import Path
from typing import Any

from autorefactor.document import get_pack, is_replaceable_doc, looks_documented
from autorefactor.document.packs.base import AUTOREFACTOR_DOC_MARKER
from autorefactor.domain import CodeSpan, CodeUnit, DocumentGap, UnitKind, content_hash, unit_id
from autorefactor.enrich.describe import describe_units
from autorefactor.index.store import IndexStore
from autorefactor.providers.base import LLMProvider
from autorefactor.stages.document import write_unit_doc


def _unit(
    *,
    key: str,
    path: str,
    qn: str,
    language: str,
    snippet: str,
    kind: UnitKind = UnitKind.FUNCTION,
    description: str | None = None,
    repo: str = "demo",
) -> CodeUnit:
    return CodeUnit(
        id=unit_id(key),
        repo=repo,
        path=path,
        qualified_name=qn,
        kind=kind,
        language=language,
        source_snippet=snippet,
        content_hash=content_hash(snippet),
        span=CodeSpan(start_line=1, end_line=snippet.count("\n") + 1),
        description=description,
    )


def test_java_wrap_extract_round_trip() -> None:
    pack = get_pack("java")
    assert pack is not None
    inner = f"Charges the wallet balance. ({AUTOREFACTOR_DOC_MARKER})"
    wrapped = pack.wrap(inner).replace("{PREFIX}", "  ")
    snippet = wrapped + "  public void charge() {\n    return;\n  }\n"
    extracted = pack.extract(snippet)
    assert extracted is not None
    assert "Charges the wallet" in extracted
    assert AUTOREFACTOR_DOC_MARKER in extracted


def test_python_wrap_extract_round_trip() -> None:
    pack = get_pack("python")
    assert pack is not None
    inner = f"Normalize a user id. ({AUTOREFACTOR_DOC_MARKER})"
    wrapped = pack.wrap(inner).replace("{INDENT}", "    ")
    snippet = "def normalize(x):\n" + wrapped + "    return x\n"
    extracted = pack.extract(snippet)
    assert extracted is not None
    assert "Normalize a user id" in extracted


def test_replaceable_todo_and_marker_not_human() -> None:
    pack = get_pack("java")
    assert pack is not None
    todo = "/** TODO: characterize Foo.bar. */\npublic void bar() {}\n"
    owned = f"/** Does work. ({AUTOREFACTOR_DOC_MARKER}) */\npublic void bar() {{}}\n"
    human = "/** Human-owned payment settlement helper. */\npublic void bar() {}\n"
    assert pack.is_replaceable(todo) is True
    assert pack.is_replaceable(owned) is True
    assert pack.is_replaceable(human) is False
    assert pack.is_replaceable("public void bare() {}\n") is True

    human_unit = _unit(
        key="h",
        path="Foo.java",
        qn="Foo.bar",
        language="java",
        snippet=human,
    )
    assert looks_documented(human_unit) is True
    assert is_replaceable_doc(human_unit) is False


def test_describe_seeds_from_source_without_provider(tmp_path: Path) -> None:
    class BoomProvider(LLMProvider):
        def chat_json(self, system: str, user: str, *, temperature: float = 0.2) -> dict:
            raise AssertionError("provider should not be called when source docs exist")

        def embed_texts(self, texts: list[str]) -> list[list[float]]:
            return [[0.0] * 3 for _ in texts]

        def describe_unit(self, **kwargs: Any):  # type: ignore[no-untyped-def]
            raise AssertionError("describe_unit should not be called")

    snippet = (
        f"/** Settles a bonus ledger entry. ({AUTOREFACTOR_DOC_MARKER}) */\n"
        "public void settle() {\n  save();\n}\n"
    )
    unit = _unit(key="s", path="Svc.java", qn="Svc.settle", language="java", snippet=snippet)
    store = IndexStore(tmp_path / "index.db")
    out = describe_units([unit], BoomProvider(), store, concurrency=1)
    store.close()
    assert out[0].description is not None
    assert "Settles a bonus" in out[0].description
    assert "from_source" in out[0].tags


def test_write_unit_doc_inserts_description_not_todo(tmp_path: Path) -> None:
    repo = tmp_path / "demo"
    src = repo / "src" / "Foo.java"
    src.parent.mkdir(parents=True)
    src.write_text(
        "public class Foo {\n  public void charge(int amount) {\n    return;\n  }\n}\n",
        encoding="utf-8",
    )
    unit = _unit(
        key="c",
        path="src/Foo.java",
        qn="Foo.charge",
        language="java",
        snippet="public void charge(int amount) {\n    return;\n}\n",
        description=f"Applies a charge to the account. ({AUTOREFACTOR_DOC_MARKER})",
    )
    gap = DocumentGap(
        unit_id=unit.id,
        repo="demo",
        path="src/Foo.java",
        qualified_name=unit.qualified_name,
        language="java",
        kind="function",
        reason="missing",
    )
    touched = write_unit_doc(tmp_path, gap, unit)
    assert touched == "demo/src/Foo.java"
    text = src.read_text(encoding="utf-8")
    assert "Applies a charge" in text
    assert "TODO: characterize" not in text
    assert AUTOREFACTOR_DOC_MARKER in text
    assert "/**" in text


def test_write_unit_doc_single_root_uses_flat_path(tmp_path: Path) -> None:
    """When repo name equals workspace folder name, do not nest under workspace/repo/."""
    src = tmp_path / "src" / "Foo.java"
    src.parent.mkdir(parents=True)
    src.write_text(
        "public class Foo {\n  public void charge(int amount) {\n    return;\n  }\n}\n",
        encoding="utf-8",
    )
    repo_name = tmp_path.name
    unit = _unit(
        key="flat",
        path="src/Foo.java",
        qn="Foo.charge",
        language="java",
        snippet="public void charge(int amount) {\n    return;\n}\n",
        description=f"Applies a charge to the account. ({AUTOREFACTOR_DOC_MARKER})",
        repo=repo_name,
    )
    gap = DocumentGap(
        unit_id=unit.id,
        repo=repo_name,
        path="src/Foo.java",
        qualified_name=unit.qualified_name,
        language="java",
        kind="function",
        reason="missing",
    )
    touched = write_unit_doc(tmp_path, gap, unit)
    assert touched == f"{repo_name}/src/Foo.java"
    assert not (tmp_path / repo_name / "src" / "Foo.java").exists()
    text = src.read_text(encoding="utf-8")
    assert "Applies a charge" in text
    assert AUTOREFACTOR_DOC_MARKER in text


def test_write_unit_doc_above_annotations(tmp_path: Path) -> None:
    repo = tmp_path / "demo"
    src = repo / "src" / "Foo.java"
    src.parent.mkdir(parents=True)
    src.write_text(
        "public class Foo {\n"
        "  @Service\n"
        "  @Override\n"
        "  public void charge(int amount) {\n"
        "    return;\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    unit = _unit(
        key="ann",
        path="src/Foo.java",
        qn="Foo.charge",
        language="java",
        snippet="@Override\npublic void charge(int amount) {\n    return;\n}\n",
        description=f"Applies a charge to the account. ({AUTOREFACTOR_DOC_MARKER})",
    )
    gap = DocumentGap(
        unit_id=unit.id,
        repo="demo",
        path="src/Foo.java",
        qualified_name=unit.qualified_name,
        language="java",
        kind="function",
        reason="missing",
    )
    assert write_unit_doc(tmp_path, gap, unit) is not None
    text = src.read_text(encoding="utf-8")
    doc_pos = text.index("Applies a charge")
    service_pos = text.index("@Service")
    override_pos = text.index("@Override")
    sig_pos = text.index("public void charge")
    assert doc_pos < service_pos < override_pos < sig_pos


def test_write_unit_doc_replace_above_annotations_no_stack(tmp_path: Path) -> None:
    repo = tmp_path / "demo"
    src = repo / "src" / "Foo.java"
    src.parent.mkdir(parents=True)
    old = f"Applies old charge. ({AUTOREFACTOR_DOC_MARKER})"
    src.write_text(
        "public class Foo {\n"
        f"  /** {old} */\n"
        "  @Service\n"
        "  @Override\n"
        "  public void charge(int amount) {\n"
        "    return;\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    snippet = (
        f"/** {old} */\n@Service\n@Override\n"
        "public void charge(int amount) {\n    return;\n}\n"
    )
    unit = _unit(
        key="rep",
        path="src/Foo.java",
        qn="Foo.charge",
        language="java",
        snippet=snippet,
        description=f"Applies a new charge. ({AUTOREFACTOR_DOC_MARKER})",
    )
    gap = DocumentGap(
        unit_id=unit.id,
        repo="demo",
        path="src/Foo.java",
        qualified_name=unit.qualified_name,
        language="java",
        kind="function",
        reason="replaceable",
    )
    assert write_unit_doc(tmp_path, gap, unit) is not None
    text = src.read_text(encoding="utf-8")
    assert text.count(AUTOREFACTOR_DOC_MARKER) == 1
    assert "Applies a new charge" in text
    assert "Applies old charge" not in text
    assert text.index("Applies a new charge") < text.index("@Service")


def test_write_unit_doc_above_multiline_annotation(tmp_path: Path) -> None:
    repo = tmp_path / "demo"
    src = repo / "src" / "Foo.java"
    src.parent.mkdir(parents=True)
    src.write_text(
        "public class Foo {\n"
        "  @Transactional(\n"
        "      readOnly = true\n"
        "  )\n"
        "  public void charge(int amount) {\n"
        "    return;\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    unit = _unit(
        key="ml",
        path="src/Foo.java",
        qn="Foo.charge",
        language="java",
        snippet="@Transactional(\n    readOnly = true\n)\npublic void charge(int amount) {\n    return;\n}\n",
        description=f"Applies a charge to the account. ({AUTOREFACTOR_DOC_MARKER})",
    )
    gap = DocumentGap(
        unit_id=unit.id,
        repo="demo",
        path="src/Foo.java",
        qualified_name=unit.qualified_name,
        language="java",
        kind="function",
        reason="missing",
    )
    assert write_unit_doc(tmp_path, gap, unit) is not None
    text = src.read_text(encoding="utf-8")
    assert text.index("Applies a charge") < text.index("@Transactional")
