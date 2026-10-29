from __future__ import annotations

import re
from pathlib import Path

from autorefactor.extract.base import iter_source_files, make_unit
from autorefactor.models import CodeUnit, UnitKind

_CREATE_TABLE = re.compile(
    r"create\s+table\s+(?:if\s+not\s+exists\s+)?([\w.\"]+)\s*\(",
    re.IGNORECASE,
)
_CREATE_POLICY = re.compile(
    r"create\s+(?:or\s+replace\s+)?policy\s+(\"?\w+\"?)\s+on\s+([\w.\"]+)",
    re.IGNORECASE,
)
_CREATE_FUNCTION = re.compile(
    r"create\s+(?:or\s+replace\s+)?(?:function|procedure)\s+([\w.\"]+)\s*\(",
    re.IGNORECASE,
)
_CREATE_TRIGGER = re.compile(
    r"create\s+(?:or\s+replace\s+)?trigger\s+(\"?\w+\"?)",
    re.IGNORECASE,
)
_CREATE_INDEX = re.compile(
    r"create\s+(?:unique\s+|concurrently\s+)*index\s+(?:if\s+not\s+exists\s+)?"
    r"(\"?\w+\"?)\s+on\s+([\w.\"]+)",
    re.IGNORECASE,
)
_DOLLAR_TAG = re.compile(r"\$([A-Za-z_]*)\$")


def _paren_block(text: str, open_idx: int) -> str:
    depth = 0
    for i in range(open_idx, len(text)):
        char = text[i]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx : i + 1]
    return text[open_idx:]


def _semicolon_statement(text: str, start: int) -> str:
    end = text.find(";", start)
    return text[start:] if end == -1 else text[start : end + 1]


def _function_block(text: str, start: int) -> str:
    """CREATE FUNCTION body: dollar-quoted ($$…$$) or single-statement fallback."""
    tag = _DOLLAR_TAG.search(text, start)
    if tag:
        close = text.find(tag.group(0), tag.end())
        end = close + len(tag.group(0)) if close != -1 else len(text)
        return text[start:end]
    return _semicolon_statement(text, start)


def _clean_name(raw: str) -> str:
    return raw.strip('"').strip()


class SqlExtractor:
    """Extract schema units (tables, RLS policies, functions, triggers, indexes) from SQL."""

    language = "sql"
    suffixes = (".sql",)

    def extract(self, repo_name: str, repo_path: Path) -> list[CodeUnit]:
        units: list[CodeUnit] = []
        for file_path in iter_source_files(repo_path, self.suffixes):
            units.extend(self._extract_file(repo_name, repo_path, file_path))
        return units

    def _extract_file(
        self,
        repo_name: str,
        repo_path: Path,
        file_path: Path,
    ) -> list[CodeUnit]:
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        rel = str(file_path.relative_to(repo_path))
        stem = file_path.stem
        module = make_unit(
            repo=repo_name,
            path=rel,
            qualified_name=stem,
            kind=UnitKind.MODULE,
            language=self.language,
            snippet=f"sql {stem}",
        )
        file_unit = make_unit(
            repo=repo_name,
            path=rel,
            qualified_name=rel,
            kind=UnitKind.FILE,
            language=self.language,
            snippet=text,
            parent_id=module.id,
            end_line=text.count("\n") + 1,
        )
        units: list[CodeUnit] = [module, file_unit]

        def add(kind: UnitKind, name: str, snippet: str) -> None:
            units.append(
                make_unit(
                    repo=repo_name,
                    path=rel,
                    qualified_name=f"{stem}.{_clean_name(name)}",
                    kind=kind,
                    language=self.language,
                    snippet=snippet,
                    parent_id=file_unit.id,
                )
            )

        for match in _CREATE_TABLE.finditer(text):
            open_idx = text.find("(", match.start())
            if open_idx == -1:
                block = match.group(0)
                snippet = block
            else:
                block = _paren_block(text, open_idx)
                # Keep the `create table …` header so schema-qualified names are recoverable.
                snippet = text[match.start() : open_idx + len(block)]
            add(UnitKind.SQL_TABLE, match.group(1), snippet)

        for match in _CREATE_POLICY.finditer(text):
            add(UnitKind.SQL_POLICY, match.group(1), _semicolon_statement(text, match.start()))

        for match in _CREATE_FUNCTION.finditer(text):
            add(UnitKind.SQL_FUNCTION, match.group(1), _function_block(text, match.start()))

        for match in _CREATE_TRIGGER.finditer(text):
            add(UnitKind.SQL_TRIGGER, match.group(1), _semicolon_statement(text, match.start()))

        for match in _CREATE_INDEX.finditer(text):
            add(UnitKind.SQL_INDEX, match.group(1), _semicolon_statement(text, match.start()))

        return units
