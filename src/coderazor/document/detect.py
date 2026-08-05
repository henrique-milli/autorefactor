from __future__ import annotations

import re
from pathlib import Path

from coderazor.models import CodeUnit, DocumentGap, UnitKind


_DOCSTRING_RE = re.compile(r'^\s*(?:async\s+)?(?:def|class)\s+\w+[\s\S]*?:\s*(?:"""|\'\'\')', re.M)
_JSDOC_RE = re.compile(r"/\*\*[\s\S]*?\*/\s*(?:export\s+)?(?:async\s+)?(?:function|class|const|let|var)")
_JAVADOC_RE = re.compile(r"/\*\*[\s\S]*?\*/\s*(?:(?:public|private|protected|static|final|synchronized|abstract|default|\s)+)?(?:class|interface|enum|record|[\w<>\[\]]+\s+\w+\s*\()")


def _looks_documented(unit: CodeUnit) -> bool:
    snippet = unit.source_snippet or ""
    lang = unit.language.lower()
    if lang == "python":
        # AST-ish: docstring as first statement after def/class header
        if '"""' in snippet[:400] or "'''" in snippet[:400]:
            return True
        return bool(_DOCSTRING_RE.search(snippet[:500]))
    if lang in {"typescript", "javascript"}:
        if snippet.lstrip().startswith("/**"):
            return True
        return bool(_JSDOC_RE.search(snippet[:800]))
    if lang == "java":
        if snippet.lstrip().startswith("/**"):
            return True
        return bool(_JAVADOC_RE.search(snippet[:1200]))
    return True  # unknown languages: don't nag


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
        if _looks_documented(unit):
            continue
        gaps.append(
            DocumentGap(
                unit_id=unit.id,
                repo=unit.repo,
                path=unit.path,
                qualified_name=unit.qualified_name,
                language=unit.language,
                kind=unit.kind.value,
                reason="Missing in-source documentation (Javadoc/JSDoc/docstring)",
            )
        )
    return sorted(gaps, key=lambda g: (g.repo, g.path, g.qualified_name))
