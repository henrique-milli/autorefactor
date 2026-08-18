from __future__ import annotations

import re
from pathlib import Path

from autorefactor.document.packs import get_pack
from autorefactor.document.packs.base import AUTOREFACTOR_DOC_MARKER, common_replaceable
from autorefactor.models import CodeUnit, DocumentGap, UnitKind


_DOCSTRING_RE = re.compile(r'^\s*(?:async\s+)?(?:def|class)\s+\w+[\s\S]*?:\s*(?:"""|\'\'\')', re.M)
_JSDOC_RE = re.compile(r"/\*\*[\s\S]*?\*/\s*(?:export\s+)?(?:async\s+)?(?:function|class|const|let|var)")
_JAVADOC_RE = re.compile(
    r"/\*\*[\s\S]*?\*/\s*(?:(?:public|private|protected|static|final|synchronized|abstract|default|\s)+)?"
    r"(?:class|interface|enum|record|[\w<>\[\]]+\s+\w+\s*\()"
)


def looks_documented(unit: CodeUnit) -> bool:
    """True when the unit snippet appears to have language-native docs."""
    snippet = unit.source_snippet or ""
    pack = get_pack(unit.language)
    if pack is not None and pack.extract(snippet):
        return True
    lang = unit.language.lower()
    if lang == "python":
        if '"""' in snippet[:400] or "'''" in snippet[:400]:
            return True
        return bool(_DOCSTRING_RE.search(snippet[:500]))
    if lang in {"typescript", "javascript", "tsx", "jsx"}:
        if snippet.lstrip().startswith("/**"):
            return True
        return bool(_JSDOC_RE.search(snippet[:800]))
    if lang == "java":
        if snippet.lstrip().startswith("/**"):
            return True
        return bool(_JAVADOC_RE.search(snippet[:1200]))
    if lang == "go":
        return bool(re.match(r"(?:[ \t]*//[^\n]*\n)+", snippet.lstrip()))
    return True  # unknown languages: don't nag


# Back-compat alias
_looks_documented = looks_documented


def is_replaceable_doc(unit: CodeUnit) -> bool:
    """Missing, TODO stub, or autorefactor-owned docs may be (re)written."""
    snippet = unit.source_snippet or ""
    pack = get_pack(unit.language)
    if pack is not None:
        return pack.is_replaceable(snippet)
    return common_replaceable(snippet, has_doc=looks_documented(unit))


def find_document_gaps(
    units: list[CodeUnit],
    *,
    languages: set[str],
) -> list[DocumentGap]:
    gaps: list[DocumentGap] = []
    for unit in units:
        if unit.language.lower() not in languages:
            continue
        if unit.kind not in {UnitKind.FUNCTION, UnitKind.TYPE}:
            continue
        name = Path(unit.path).name
        if name in {"__init__.py", "index.ts", "index.js"}:
            continue
        simple = unit.qualified_name.rsplit(".", 1)[-1]
        if simple.startswith("_") and unit.language == "python":
            continue  # private
        documented = looks_documented(unit)
        replaceable = is_replaceable_doc(unit)
        # Human docs that are not replaceable: skip. Missing or owned stubs: gap.
        if documented and not replaceable:
            continue
        if documented and replaceable:
            reason = (
                f"Replaceable in-source documentation ({AUTOREFACTOR_DOC_MARKER} or TODO stub)"
            )
        else:
            reason = "Missing in-source documentation (Javadoc/JSDoc/docstring)"
        gaps.append(
            DocumentGap(
                unit_id=unit.id,
                repo=unit.repo,
                path=unit.path,
                qualified_name=unit.qualified_name,
                language=unit.language,
                kind=unit.kind.value,
                reason=reason,
            )
        )
    return sorted(gaps, key=lambda g: (g.repo, g.path, g.qualified_name))
