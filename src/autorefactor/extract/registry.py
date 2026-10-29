from __future__ import annotations

from pathlib import Path

from autorefactor.extract.base import LanguageExtractor
from autorefactor.extract.go import GoExtractor
from autorefactor.extract.java import JavaExtractor
from autorefactor.extract.proto import ProtoExtractor
from autorefactor.extract.python_lang import PythonExtractor
from autorefactor.extract.sql import SqlExtractor
from autorefactor.extract.typescript import TypeScriptExtractor
from autorefactor.models import CodeUnit


def default_extractors() -> list[LanguageExtractor]:
    return [
        JavaExtractor(),
        TypeScriptExtractor(),
        PythonExtractor(),
        GoExtractor(),
        ProtoExtractor(),
        SqlExtractor(),
    ]


def extract_repo(repo_path: Path, extractors: list[LanguageExtractor] | None = None) -> list[CodeUnit]:
    extractors = extractors or default_extractors()
    repo_name = repo_path.name
    units: list[CodeUnit] = []
    for extractor in extractors:
        units.extend(extractor.extract(repo_name, repo_path))
    return units


def extract_workspace(repos: list[Path]) -> list[CodeUnit]:
    units: list[CodeUnit] = []
    for repo in repos:
        units.extend(extract_repo(repo))
    return units
