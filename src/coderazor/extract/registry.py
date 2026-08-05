from __future__ import annotations

from pathlib import Path

from coderazor.extract.base import LanguageExtractor
from coderazor.extract.go import GoExtractor
from coderazor.extract.java import JavaExtractor
from coderazor.extract.proto import ProtoExtractor
from coderazor.extract.python_lang import PythonExtractor
from coderazor.extract.typescript import TypeScriptExtractor
from coderazor.models import CodeUnit


def default_extractors() -> list[LanguageExtractor]:
    return [
        JavaExtractor(),
        TypeScriptExtractor(),
        PythonExtractor(),
        GoExtractor(),
        ProtoExtractor(),
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
