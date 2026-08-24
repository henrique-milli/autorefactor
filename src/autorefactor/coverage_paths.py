"""Normalize and match coverage report paths against unit paths."""

from __future__ import annotations

_JAVA_SRC_PREFIXES = (
    "src/main/java/",
    "src/test/java/",
    "src/main/kotlin/",
    "src/test/kotlin/",
)


def strip_repo_prefix(path: str, repo: str | None = None) -> str:
    normalized = (path or "").replace("\\", "/").lstrip("./")
    if repo:
        prefix = f"{repo}/"
        if normalized == repo:
            return ""
        if normalized.startswith(prefix):
            return normalized[len(prefix) :]
    return normalized


def strip_source_root(path: str) -> str:
    """Strip Maven/Gradle source roots so package/file remains."""
    normalized = (path or "").replace("\\", "/").lstrip("./")
    for prefix in _JAVA_SRC_PREFIXES:
        if normalized.startswith(prefix):
            return normalized[len(prefix) :]
    return normalized


def normalize_jacoco_rel_path(path: str) -> str:
    """Map Jacoco package/file paths to Maven ``src/main/java/...`` form.

    Jacoco XML yields ``com/foo/Bar.java`` (no source root). Units use
    ``src/main/java/com/foo/Bar.java``. Leave paths that already include ``src/`` alone.
    """
    normalized = (path or "").replace("\\", "/").lstrip("./")
    if not normalized:
        return normalized
    if "/src/" in f"/{normalized}" or normalized.startswith("src/"):
        return normalized
    if normalized.endswith((".java", ".kt", ".kts")):
        return f"src/main/java/{normalized}"
    return normalized


def coverage_path_matches(
    unit_path: str,
    report_path: str,
    *,
    repo: str | None = None,
) -> bool:
    """True when a coverage file row refers to the same source as ``unit_path``."""
    unit = strip_source_root(strip_repo_prefix(unit_path, repo))
    report = strip_source_root(strip_repo_prefix(report_path, repo))
    if not unit or not report:
        return False
    if unit == report:
        return True
    if unit.endswith("/" + report) or report.endswith("/" + unit):
        return True
    if unit.endswith(report) or report.endswith(unit):
        # Require a path separator boundary when one is a suffix of the other
        longer, shorter = (unit, report) if len(unit) >= len(report) else (report, unit)
        if longer.endswith(shorter) and (
            len(longer) == len(shorter) or longer[-len(shorter) - 1] == "/"
        ):
            return True
    return False
