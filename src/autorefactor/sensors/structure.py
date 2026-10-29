from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from autorefactor.domain import (
    AnalysisGoal,
    CodeUnit,
    Finding,
    Severity,
    SuggestedAction,
    UnitKind,
    content_hash,
    try_make_finding,
)
from autorefactor.extract.base import iter_source_files
from autorefactor.index.hierarchy import simple_name
from autorefactor.sensors.anchors import production_type_for_repo, resolve_file_unit
from autorefactor.sensors.base import AnalysisContext
from autorefactor.sensors.similarity import LOW_SIGNAL_NAMES, is_test_path

_TRIVIAL_DUP_NAMES = LOW_SIGNAL_NAMES | frozenset(
    {
        "main",
        "init",
        "shutdown",
        "start",
        "stop",
        "close",
        "run",
        "execute",
        "apply",
        "build",
        "create",
        "update",
        "delete",
        "save",
        "load",
        "get",
        "set",
        "valueof",
        "builder",
    }
)

_DTO_CONVERT_NAME = re.compile(
    r"^(to[A-Z].*|mapTo.*|convertTo.*|fromProto.*|toProto.*|of[A-Z].*)$"
)
_CATCH_RE = re.compile(r"\bcatch\s*\(|\bexcept\s+", re.IGNORECASE)
_MIN_LONG_FN_LOC = 40


def _fn_lines(unit: CodeUnit) -> int:
    lines = unit.span.end_line - unit.span.start_line + 1
    if unit.span.end_line == 0:
        lines = (unit.source_snippet or "").count("\n") + 1
    return max(0, lines)


def _is_boilerplate_fn(unit: CodeUnit) -> bool:
    """DTO converters and catch-dominated HTTP wrappers are weak split targets."""
    name = simple_name(unit.qualified_name)
    if _DTO_CONVERT_NAME.match(name):
        return True
    snippet = unit.source_snippet or ""
    lines = max(1, snippet.count("\n") + 1)
    catches = len(_CATCH_RE.findall(snippet))
    if catches >= 2 and catches * 8 >= lines:
        return True
    return False


def _large_file_second_signal(
    file_unit: CodeUnit,
    units: list[CodeUnit],
    lines: int,
    max_file: int,
) -> str | None:
    """Return evidence label for a second cohesion signal, or None if size-only."""
    if lines >= max_file * 2:
        return f"extreme size {lines} lines (>= {max_file * 2})"
    path = file_unit.path
    repo = file_unit.repo
    children = [
        u
        for u in units
        if u.repo == repo
        and u.path == path
        and u.kind in {UnitKind.FUNCTION, UnitKind.TYPE}
        and u.id != file_unit.id
    ]
    if len(children) >= 12:
        return f"{len(children)} child functions/types in file"
    types = [u for u in children if u.kind == UnitKind.TYPE]
    # Also count types whose parent is this file
    types_on_path = [
        u
        for u in units
        if u.repo == repo and u.path == path and u.kind == UnitKind.TYPE
    ]
    if len(types_on_path) >= 2:
        return f"{len(types_on_path)} top-level types in file"
    if len(types) >= 2:
        return f"{len(types)} nested types in file"
    return None


class StructureSensor:
    """Long units, large files, god modules, cross-repo duplicate names."""

    id = "structure"
    goal = AnalysisGoal.STRUCTURE

    def available(self, ctx: AnalysisContext) -> bool:
        return True

    def collect(self, ctx: AnalysisContext) -> list[Finding]:
        known = ctx.known_ids()
        max_fn = ctx.config.long_function_lines
        max_file = ctx.config.large_file_lines
        if ctx.focus == "strict":
            max_fn = min(max_fn, 50)
            max_file = min(max_file, 400)
        effective_fn = max(max_fn, _MIN_LONG_FN_LOC)
        findings: list[Finding] = []
        units = list(ctx.units)

        for unit in units:
            if unit.kind == UnitKind.FUNCTION:
                if is_test_path(unit.path):
                    continue
                if _is_boilerplate_fn(unit):
                    continue
                lines = _fn_lines(unit)
                if lines >= effective_fn:
                    f = try_make_finding(
                        id=content_hash(f"long:{unit.id}:{lines}"),
                        goal=self.goal,
                        sensor=self.id,
                        severity=Severity.MEDIUM if lines < effective_fn * 2 else Severity.HIGH,
                        title=f"Long function {unit.qualified_name}",
                        evidence=f"{lines} lines (threshold {effective_fn})",
                        unit_ids=[unit.id],
                        action_hint=SuggestedAction.SPLIT,
                        score=min(1.0, lines / (effective_fn * 2)),
                        metrics={"lines": float(lines)},
                        known_ids=known,
                    )
                    if f:
                        findings.append(f)
            elif unit.kind == UnitKind.FILE:
                if is_test_path(unit.path):
                    continue
                lines = unit.span.end_line or ((unit.source_snippet or "").count("\n") + 1)
                if lines < max_file:
                    continue
                second = _large_file_second_signal(unit, units, lines, max_file)
                if second is None:
                    continue
                f = try_make_finding(
                    id=content_hash(f"largefile:{unit.id}:{lines}"),
                    goal=self.goal,
                    sensor=self.id,
                    severity=Severity.MEDIUM if lines < max_file * 2 else Severity.HIGH,
                    title=f"Large file {unit.path}",
                    evidence=f"{lines} lines (threshold {max_file}); second signal: {second}",
                    unit_ids=[unit.id],
                    action_hint=SuggestedAction.SPLIT,
                    score=min(1.0, lines / (max_file * 2)),
                    metrics={"lines": float(lines)},
                    metadata={"second_signal": second},
                    known_ids=known,
                )
                if f:
                    findings.append(f)

        # God modules
        children: dict[str, list[CodeUnit]] = defaultdict(list)
        by_id = {u.id: u for u in units}
        min_children = 25
        for unit in units:
            if unit.parent_id and unit.kind in {UnitKind.FUNCTION, UnitKind.TYPE}:
                children[unit.parent_id].append(unit)
        for parent_id, kids in children.items():
            if len(kids) < min_children:
                continue
            parent = by_id.get(parent_id)
            if not parent or parent.kind not in {UnitKind.MODULE, UnitKind.FILE, UnitKind.TYPE}:
                continue
            f = try_make_finding(
                id=content_hash(f"god:{parent_id}:{len(kids)}"),
                goal=self.goal,
                sensor=self.id,
                severity=Severity.HIGH if len(kids) > min_children * 2 else Severity.MEDIUM,
                title=f"Oversized module/type {parent.qualified_name}",
                evidence=f"{len(kids)} child functions/types (threshold {min_children})",
                unit_ids=[parent_id, *[k.id for k in kids[:20]]],
                action_hint=SuggestedAction.SPLIT,
                score=min(1.0, len(kids) / (min_children * 2)),
                metrics={"child_count": float(len(kids))},
                known_ids=known,
            )
            if f:
                findings.append(f)

        # Cross-repo duplicate names (skip ubiquitous Spring/Java entrypoints)
        by_name: dict[str, list[CodeUnit]] = defaultdict(list)
        for unit in units:
            if unit.kind not in {
                UnitKind.FUNCTION,
                UnitKind.TYPE,
                UnitKind.PROTO_MESSAGE,
                UnitKind.PROTO_RPC,
            }:
                continue
            if is_test_path(unit.path):
                continue
            name = simple_name(unit.qualified_name)
            if name.lower() in _TRIVIAL_DUP_NAMES:
                continue
            by_name[name].append(unit)
        for name, group in by_name.items():
            repos = {u.repo for u in group}
            if len(repos) < 2:
                continue
            f = try_make_finding(
                id=content_hash(f"dupname:{name}:{','.join(sorted(u.id for u in group))}"),
                goal=self.goal,
                sensor=self.id,
                severity=Severity.MEDIUM,
                title=f"Duplicate name '{name}' across repos",
                evidence=f"Found in repos: {', '.join(sorted(repos))}",
                unit_ids=[u.id for u in group],
                action_hint=SuggestedAction.EXTRACT_SHARED,
                score=0.7,
                metrics={},
                known_ids=known,
            )
            if f:
                findings.append(f)

        # Polyglot workspace hygiene
        findings.extend(_polyglot_findings(units, ctx, known))
        findings.extend(_repo_hygiene_findings(units, ctx, known))

        return findings


_ORCHESTRATION_FILES = (
    "Makefile",
    "package.json",
    "go.mod",
    "pyproject.toml",
    "pom.xml",
    "build.gradle",
)
_README_NAMES = ("README.md", "README", "readme.md")
_POLYglot_LANGS = frozenset({"python", "go", "typescript", "tsx", "javascript", "java"})


def _repo_languages(units: list[CodeUnit], repo: str) -> set[str]:
    langs: set[str] = set()
    for u in units:
        if u.repo == repo and not is_test_path(u.path) and u.language in _POLYglot_LANGS:
            langs.add(u.language)
    return langs


def _has_orchestration(repo_path: Path) -> bool:
    if any((repo_path / name).is_file() for name in _ORCHESTRATION_FILES):
        return True
    pyproject = repo_path / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8", errors="replace")
        if "[tool.pytest" in text:
            return True
    return False


def _has_readme(repo_path: Path) -> bool:
    return any((repo_path / name).is_file() for name in _README_NAMES)


def _core_symbols(units: list[CodeUnit], repo: str) -> set[str]:
    names: set[str] = set()
    for u in units:
        if u.repo != repo or is_test_path(u.path):
            continue
        if u.kind in {UnitKind.FUNCTION, UnitKind.TYPE}:
            n = simple_name(u.qualified_name).lower()
            if n not in _TRIVIAL_DUP_NAMES:
                names.add(n)
    return names


def _polyglot_findings(
    units: list[CodeUnit],
    ctx: AnalysisContext,
    known: set[str],
) -> list[Finding]:
    findings: list[Finding] = []
    repos = sorted({u.repo for u in units})
    for repo_name in repos:
        langs = _repo_languages(units, repo_name)
        if len(langs) < 2:
            continue
        repo_path = next((p for p in ctx.repos if p.name == repo_name), None)
        if repo_path is None:
            continue
        has_readme = _has_readme(repo_path)
        has_build = _has_orchestration(repo_path)
        if not has_readme or not has_build:
            repo_units = [u for u in units if u.repo == repo_name]
            uids = [
                u.id
                for u in repo_units
                if u.kind == UnitKind.TYPE and not is_test_path(u.path)
            ][:1] or [u.id for u in repo_units if u.kind == UnitKind.FILE][:1]
            if not uids:
                continue
            f = try_make_finding(
                id=content_hash(f"structure:polyglot:{repo_name}"),
                goal=AnalysisGoal.STRUCTURE,
                sensor="structure",
                severity=Severity.MEDIUM,
                title=f"Polyglot repo `{repo_name}` lacks layout docs or build entrypoint",
                evidence=(
                    f"Hosts {', '.join(sorted(langs))} without "
                    f"{'README ' if not has_readme else ''}"
                    f"{'unified build orchestration' if not has_build else ''} — "
                    "add README describing layout and Makefile/package.json/go.mod/pyproject."
                ).replace("without unified", "without").replace("without  ", "without "),
                unit_ids=uids,
                action_hint=SuggestedAction.DOCUMENT,
                score=0.7,
                metadata={"languages": sorted(langs), "has_readme": has_readme, "has_build": has_build},
                known_ids=known,
            )
            if f:
                findings.append(f)

        core = _core_symbols(units, repo_name)
        for u in units:
            if u.repo != repo_name or u.language != "java" or u.kind != UnitKind.TYPE:
                continue
            if is_test_path(u.path):
                continue
            sym = simple_name(u.qualified_name).lower()
            if sym in core:
                continue
            f = try_make_finding(
                id=content_hash(f"structure:orphan:{u.id}"),
                goal=AnalysisGoal.STRUCTURE,
                sensor="structure",
                severity=Severity.MEDIUM,
                title=f"Orphan sample `{u.qualified_name}` unrelated to core util",
                evidence=(
                    f"`{u.path}` has no naming overlap with core symbols "
                    f"({', '.join(sorted(core)[:6])}) — consider relocation or removal."
                ),
                unit_ids=[u.id],
                action_hint=SuggestedAction.MOVE,
                score=0.65,
                metadata={"kind": "orphan_sample"},
                known_ids=known,
            )
            if f:
                findings.append(f)
    return findings


_MONGO_SECRET = re.compile(
    r"mongodb(?:\+srv)?://[^@\s/${][^@\s]*@|password\s*=\s*(?!['\"]?\$\{)[^#\s'\"]{4,}",
    re.IGNORECASE,
)
_PLACEHOLDER = re.compile(r"\$\{[^}]+\}")
_BUILD_VERB_MVN = re.compile(r"\bmvn\b|\bmaven\b", re.IGNORECASE)
_BUILD_VERB_GRADLE = re.compile(r"\bgradle\b|\bgradlew\b", re.IGNORECASE)
_REST_PATH = re.compile(r"(?:GET|POST|PUT|DELETE|PATCH)\s+/[\w/{:-]+", re.IGNORECASE)
_MISLEADING_CATCH = re.compile(
    r"catch\s*\(\s*(?:Exception|\w+Exception)\s+\w+\s*\)\s*\{[^}]*"
    r"(?:throw\s+new\s+(\w*Client\w*Exception)|(\w*Client\w*Exception)\s*\()",
    re.IGNORECASE | re.DOTALL,
)


def _read_repo_text(path: Path, limit: int = 500_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _repo_unit_id(units: list[CodeUnit], repo_name: str) -> str | None:
    for u in units:
        if u.repo == repo_name and u.kind == UnitKind.REPO:
            return u.id
    for u in units:
        if u.repo == repo_name:
            return u.id
    return None


def _committed_secret_findings(
    ctx: AnalysisContext,
    repo_path: Path,
    repo_name: str,
    known: set[str],
) -> list[Finding]:
    findings: list[Finding] = []
    for pattern in ("**/application*.yml", "**/application*.yaml", "**/application*.properties"):
        for path in repo_path.glob(pattern):
            if not path.is_file():
                continue
            rel = str(path.relative_to(repo_path)).replace("\\", "/")
            if rel.startswith(("target/", "build/")) or "/target/" in rel:
                continue
            text = _read_repo_text(path)
            scrubbed = _PLACEHOLDER.sub("", text)
            if not _MONGO_SECRET.search(scrubbed):
                continue
            unit = resolve_file_unit(ctx, repo_name, rel)
            if unit is None:
                continue
            f = try_make_finding(
                id=content_hash(f"structure:committed_secret:{repo_name}:{rel}"),
                goal=AnalysisGoal.STRUCTURE,
                sensor="structure",
                severity=Severity.HIGH,
                title=f"Committed credential default in `{rel}` ({repo_name})",
                evidence=(
                    f"`{rel}` embeds mongodb URI credentials or password= literal (not ${{…}} placeholder) — "
                    "rotate secret and externalize via env/vault."
                ),
                unit_ids=[unit.id],
                action_hint=SuggestedAction.DOCUMENT,
                score=0.92,
                metadata={"kind": "committed_secret", "repo": repo_name, "path": rel},
                known_ids=known,
            )
            if f:
                findings.append(f)
    return findings


def _readme_consistency_findings(
    repo_path: Path,
    repo_name: str,
    unit_id: str,
    known: set[str],
) -> list[Finding]:
    findings: list[Finding] = []
    readmes: list[tuple[str, str, Path]] = []
    for path in [repo_path / "README.md", *repo_path.rglob("README*.md")]:
        if not path.is_file():
            continue
        rel = str(path.relative_to(repo_path)).replace("\\", "/")
        if "/target/" in rel or "/build/" in rel:
            continue
        readmes.append((rel, _read_repo_text(path), path))
    if len(readmes) < 2:
        return findings

    root_rel, root_text, _ = readmes[0]
    root_mvn = bool(_BUILD_VERB_MVN.search(root_text))
    root_gradle = bool(_BUILD_VERB_GRADLE.search(root_text))
    root_paths = set(_REST_PATH.findall(root_text))

    for rel, text, _ in readmes[1:]:
        nested_mvn = bool(_BUILD_VERB_MVN.search(text))
        nested_gradle = bool(_BUILD_VERB_GRADLE.search(text))
        if (root_mvn and nested_gradle and not nested_mvn) or (root_gradle and nested_mvn and not nested_gradle):
            f = try_make_finding(
                id=content_hash(f"structure:readme_build:{repo_name}:{root_rel}:{rel}"),
                goal=AnalysisGoal.STRUCTURE,
                sensor="structure",
                severity=Severity.MEDIUM,
                title=f"Conflicting build instructions between READMEs in {repo_name}",
                evidence=(
                    f"`{root_rel}` references {'mvn' if root_mvn else 'gradle'} while `{rel}` references "
                    f"{'gradle' if nested_gradle else 'mvn'} — unify build verbs for agents."
                ),
                unit_ids=[unit_id],
                action_hint=SuggestedAction.DOCUMENT,
                score=0.75,
                metadata={"kind": "readme_build_drift", "repo": repo_name, "root": root_rel, "nested": rel},
                known_ids=known,
            )
            if f:
                findings.append(f)

        nested_paths = set(_REST_PATH.findall(text))
        missing_in_nested = sorted(root_paths - nested_paths)
        missing_in_root = sorted(nested_paths - root_paths)
        if missing_in_nested or missing_in_root:
            parts: list[str] = []
            if missing_in_nested:
                parts.append(f"in `{root_rel}` but not `{rel}`: {', '.join(missing_in_nested[:6])}")
            if missing_in_root:
                parts.append(f"in `{rel}` but not `{root_rel}`: {', '.join(missing_in_root[:6])}")
            f = try_make_finding(
                id=content_hash(f"structure:readme_paths:{repo_name}:{rel}"),
                goal=AnalysisGoal.STRUCTURE,
                sensor="structure",
                severity=Severity.MEDIUM,
                title=f"REST path doc drift between READMEs in {repo_name}",
                evidence="; ".join(parts),
                unit_ids=[unit_id],
                action_hint=SuggestedAction.DOCUMENT,
                score=0.72,
                metadata={"kind": "readme_path_drift", "repo": repo_name},
                known_ids=known,
            )
            if f:
                findings.append(f)
    return findings


def _dead_api_findings(
    units: list[CodeUnit],
    repo_path: Path,
    repo_name: str,
    known: set[str],
) -> list[Finding]:
    findings: list[Finding] = []
    main_sources: dict[str, str] = {}
    for p in repo_path.rglob("*.java"):
        rel = str(p.relative_to(repo_path)).replace("\\", "/")
        if "src/main/" not in rel:
            continue
        main_sources[rel] = _read_repo_text(p)
    test_java = "\n".join(
        _read_repo_text(p)
        for p in repo_path.rglob("*.java")
        if "src/test/" in str(p.relative_to(repo_path)).replace("\\", "/") and p.is_file()
    )
    by_id = {u.id: u for u in units}
    for unit in units:
        if unit.repo != repo_name or unit.kind != UnitKind.FUNCTION:
            continue
        if is_test_path(unit.path) or "src/main/" not in unit.path.replace("\\", "/"):
            continue
        sym = simple_name(unit.qualified_name)
        if sym.lower() in _TRIVIAL_DUP_NAMES:
            continue
        unit_rel = unit.path.replace("\\", "/")
        in_test = bool(re.search(rf"\b{re.escape(sym)}\s*\(", test_java))
        in_main_elsewhere = any(
            rel != unit_rel and re.search(rf"\b{re.escape(sym)}\s*\(", text)
            for rel, text in main_sources.items()
        )
        if in_test and not in_main_elsewhere:
            parent = by_id.get(unit.parent_id) if unit.parent_id else None
            type_qn = parent.qualified_name if parent else unit.path
            f = try_make_finding(
                id=content_hash(f"structure:dead_api:{unit.id}"),
                goal=AnalysisGoal.STRUCTURE,
                sensor="structure",
                severity=Severity.MEDIUM,
                title=f"Test-only API `{unit.qualified_name}` in {repo_name}",
                evidence=(
                    f"`{sym}` on `{type_qn}` is referenced from src/test but never from src/main — "
                    "dead or test-only surface; remove or wire into production path."
                ),
                unit_ids=[unit.id],
                action_hint=SuggestedAction.DELETE_DUPLICATE,
                score=0.78,
                metadata={"kind": "test_only_api", "repo": repo_name, "symbol": sym},
                known_ids=known,
            )
            if f:
                findings.append(f)
    return findings


def _dead_api_findings_ts(
    units: list[CodeUnit],
    repo_path: Path,
    repo_name: str,
    known: set[str],
) -> list[Finding]:
    """TS/JS analog: symbol called from tests but absent from other production sources."""
    main_sources: dict[str, str] = {}
    test_sources: dict[str, str] = {}
    for path in iter_source_files(repo_path, (".ts", ".tsx", ".js", ".jsx")):
        rel = str(path.relative_to(repo_path)).replace("\\", "/")
        text = _read_repo_text(path)
        if is_test_path(rel):
            test_sources[rel] = text
        else:
            main_sources[rel] = text
    if not test_sources or not main_sources:
        return []
    test_blob = "\n".join(test_sources.values())

    findings: list[Finding] = []
    for unit in units:
        if unit.repo != repo_name or unit.kind != UnitKind.FUNCTION:
            continue
        if is_test_path(unit.path):
            continue
        if unit.language not in {"typescript", "tsx", "javascript"}:
            continue
        sym = simple_name(unit.qualified_name)
        if sym.lower() in _TRIVIAL_DUP_NAMES:
            continue
        # Test usage: a call site or JSX usage — never bare word collisions.
        if not re.search(rf"\b{re.escape(sym)}\s*\(|<{re.escape(sym)}\b", test_blob):
            continue
        unit_rel = unit.path.replace("\\", "/")
        # Production usage is lenient (word boundary) so value references count too.
        in_main_elsewhere = any(
            rel != unit_rel and re.search(rf"\b{re.escape(sym)}\b", text)
            for rel, text in main_sources.items()
        )
        if in_main_elsewhere:
            continue
        f = try_make_finding(
            id=content_hash(f"structure:dead_api_ts:{unit.id}"),
            goal=AnalysisGoal.STRUCTURE,
            sensor="structure",
            severity=Severity.MEDIUM,
            title=f"Test-only API `{unit.qualified_name}` in {repo_name}",
            evidence=(
                f"`{sym}` is called from tests but never from other production sources — "
                "dead or test-only surface; wire it into the production path or remove it."
            ),
            unit_ids=[unit.id],
            action_hint=SuggestedAction.DELETE_DUPLICATE,
            score=0.78,
            metadata={"kind": "test_only_api", "repo": repo_name, "symbol": sym},
            known_ids=known,
        )
        if f:
            findings.append(f)
    return findings


def _misleading_catch_findings(
    units: list[CodeUnit],
    repo_name: str,
    known: set[str],
) -> list[Finding]:
    findings: list[Finding] = []
    for unit in units:
        if unit.repo != repo_name or unit.kind != UnitKind.FUNCTION:
            continue
        if is_test_path(unit.path):
            continue
        path_lower = unit.path.replace("\\", "/").lower()
        qn_lower = unit.qualified_name.lower()
        if "service" not in path_lower and "service" not in qn_lower:
            if "client" not in path_lower and "client" not in qn_lower:
                continue
        snippet = unit.source_snippet or ""
        m = _MISLEADING_CATCH.search(snippet)
        if not m:
            continue
        exc_type = m.group(1) or m.group(2) or "ClientException"
        mongo_ctx = bool(re.search(r"Mongo|Repository|\.find\s*\(|mongo", snippet, re.IGNORECASE))
        ps_ctx = bool(re.search(r"PokerStars|PSN|psn|ClientException", exc_type, re.IGNORECASE))
        if mongo_ctx and ps_ctx:
            f = try_make_finding(
                id=content_hash(f"structure:misleading_catch:{unit.id}"),
                goal=AnalysisGoal.STRUCTURE,
                sensor="structure",
                severity=Severity.MEDIUM,
                title=f"Misleading error mapping in `{simple_name(unit.qualified_name)}`",
                evidence=(
                    f"`{unit.path}` catch wraps Mongo/repository failures as `{exc_type}` — "
                    "error code implies external client fault; map to persistence/infrastructure type."
                ),
                unit_ids=[unit.id],
                action_hint=SuggestedAction.DOCUMENT,
                score=0.8,
                metadata={
                    "kind": "misleading_catch",
                    "repo": repo_name,
                    "exception_type": exc_type,
                    "error_code_hint": "CLIENT_PS_001",
                },
                known_ids=known,
            )
            if f:
                findings.append(f)
    return findings


def _repo_hygiene_findings(
    units: list[CodeUnit],
    ctx: AnalysisContext,
    known: set[str],
) -> list[Finding]:
    findings: list[Finding] = []
    for repo_path in ctx.repos:
        repo_name = repo_path.name
        uid = _repo_unit_id(units, repo_name)
        findings.extend(_committed_secret_findings(ctx, repo_path, repo_name, known))
        prod = production_type_for_repo(ctx, repo_name)
        readme_uid = prod.id if prod is not None else None
        if readme_uid:
            findings.extend(_readme_consistency_findings(repo_path, repo_name, readme_uid, known))
        findings.extend(_dead_api_findings(units, repo_path, repo_name, known))
        findings.extend(_dead_api_findings_ts(units, repo_path, repo_name, known))
        findings.extend(_misleading_catch_findings(units, repo_name, known))
    return findings
