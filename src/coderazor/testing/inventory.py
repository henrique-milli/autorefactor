from __future__ import annotations

from pathlib import Path

from coderazor.config import IGNORE_DIR_NAMES

TEST_NAME_HINTS = (
    "test_",
    "_test.",
    ".test.",
    ".spec.",
    "tests/",
    "test/",
    "__tests__/",
    "spec/",
)


def looks_like_test_path(path: str) -> bool:
    lowered = path.replace("\\", "/").lower()
    return any(hint in lowered for hint in TEST_NAME_HINTS)


def inventory_test_files(repos: list[Path]) -> list[str]:
    found: list[str] = []
    suffixes = {".py", ".java", ".ts", ".tsx", ".js", ".jsx", ".go"}
    for repo in repos:
        for path in repo.rglob("*"):
            if not path.is_file():
                continue
            if any(part in IGNORE_DIR_NAMES for part in path.parts):
                continue
            if path.suffix.lower() not in suffixes:
                continue
            rel = f"{repo.name}/{path.relative_to(repo)}"
            if looks_like_test_path(rel):
                found.append(rel)
    return sorted(found)
