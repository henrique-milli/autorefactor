from __future__ import annotations

from pathlib import Path
from typing import Protocol

from autorefactor.config import IGNORE_DIR_NAMES, MAX_SOURCE_CHARS
from autorefactor.models import CodeSpan, CodeUnit, UnitKind, content_hash, unit_id


def truncate(text: str, limit: int = MAX_SOURCE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n/* ... truncated ... */"


def make_unit(
    *,
    repo: str,
    path: str,
    qualified_name: str,
    kind: UnitKind,
    language: str,
    snippet: str,
    parent_id: str | None = None,
    start_line: int = 0,
    end_line: int = 0,
    start_byte: int = 0,
    end_byte: int = 0,
) -> CodeUnit:
    snippet = truncate(snippet)
    return CodeUnit(
        id=unit_id(repo, path, kind.value, qualified_name, content_hash(snippet)),
        repo=repo,
        path=path,
        qualified_name=qualified_name,
        kind=kind,
        language=language,
        source_snippet=snippet,
        content_hash=content_hash(snippet),
        parent_id=parent_id,
        span=CodeSpan(
            start_line=start_line,
            end_line=end_line,
            start_byte=start_byte,
            end_byte=end_byte,
        ),
    )


def iter_source_files(repo_path: Path, suffixes: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for path in repo_path.rglob("*"):
        if not path.is_file():
            continue
        if any(part in IGNORE_DIR_NAMES for part in path.parts):
            continue
        if path.suffix.lower() in suffixes:
            files.append(path)
    return sorted(files)


class LanguageExtractor(Protocol):
    language: str
    suffixes: tuple[str, ...]

    def extract(self, repo_name: str, repo_path: Path) -> list[CodeUnit]:
        ...
