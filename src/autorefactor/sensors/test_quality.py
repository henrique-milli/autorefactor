"""Detect shallow autorefactor-generated tests (symbol-exists-only, duplicate boilerplate)."""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from autorefactor.domain import (
    AnalysisGoal,
    Finding,
    Severity,
    SuggestedAction,
    UnitKind,
    content_hash,
    try_make_finding,
)
from autorefactor.extract.base import iter_source_files
from autorefactor.sensors.base import AnalysisContext
from autorefactor.sensors.similarity import is_test_path

_MARKER = "(autorefactor ensure-tests)"
_SYMBOL_ONLY = re.compile(
    r"def\s+test_\w+_(symbol_exists|is_importable)\s*\("
    r"|(?:test|it)\(\s*[\"'][^\"']*symbol_exists",
    re.MULTILINE,
)
_BEHAVIOR_ASSERT = re.compile(
    r"assert\s+\w+\([^)]*\)\s*==|pytest\.mark\.parametrize|assert\s+\w+\.snapshot\(\)"
    r"|\.to(?:Equal|StrictEqual|Contain|MatchSnapshot)\(|assert\.(?:deepEqual|strictEqual)\(",
    re.MULTILINE,
)
_IMPORT_MOD = re.compile(
    r'mod\s*=\s*importlib\.import_module\(["\']([^"\']+)["\']\)',
    re.MULTILINE,
)
_CLASS_SYMBOL = re.compile(
    r'assert\s+hasattr\(mod,\s*["\'](\w+)["\']\)',
    re.MULTILINE,
)
_PER_FILE_PATH_HACK = re.compile(
    r"_REPO_ROOT\s*=\s*Path\(__file__\)\.resolve\(\)\.parents\[1\]",
    re.MULTILINE,
)
_SPRING_TEST_DEP = re.compile(
    r"spring-boot-starter-test|spring-kafka-test",
    re.IGNORECASE,
)
_INTEGRATION_ANNOT = re.compile(
    r"@SpringBootTest|@DataMongoTest|@EmbeddedKafka|@Testcontainers|org\.testcontainers",
    re.MULTILINE,
)
_JAVA_TEST_CLASS = re.compile(r"\bclass\s+(\w+Test\w*)\b")
_COVERAGE_TEST = re.compile(r"CoverageTest|coverage[- ]?padding|jacoco", re.IGNORECASE)
_GETTER_SETTER_ASSERT = re.compile(
    r"assert(?:NotNull|Equals|True|False)\([^)]*\.(get|set|is)[A-Z]\w*\("
    r"|assert\s+.*\.get[A-Z]\w*\(\)\s*!=\s*null",
    re.MULTILINE,
)
_WIRING_ASSERT = re.compile(
    r"@MockBean|MockMvc|WebMvcTest|EmbeddedKafka|MongoTemplate|KafkaTemplate|RestTemplate",
    re.MULTILINE,
)


def _read_text(path: Path, limit: int = 500_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _unit_id_for_test(ctx: AnalysisContext, rel: str) -> str | None:
    rel_lower = rel.lower()
    for u in ctx.units:
        full = f"{u.repo}/{u.path}".lower()
        if full == rel_lower or u.path.lower() == rel_lower.split("/", 1)[-1]:
            return u.id
    repo = rel.split("/", 1)[0]
    for u in ctx.units:
        if u.repo == repo:
            return u.id
    return None


def _repo_unit(ctx: AnalysisContext, repo_name: str) -> str | None:
    for u in ctx.units:
        if u.repo == repo_name and u.kind == UnitKind.REPO:
            return u.id
    for u in ctx.units:
        if u.repo == repo_name:
            return u.id
    return None


def _scan_repo_tests(repo: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for path in repo.rglob("test_*.py"):
        if not path.is_file():
            continue
        rel = f"{repo.name}/{path.relative_to(repo).as_posix()}"
        try:
            out.append((rel, path.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    # TS/JS unit tests (vitest / node:test / jest colocated or under __tests__)
    for path in iter_source_files(repo, (".ts", ".tsx", ".js", ".jsx")):
        name = path.name
        if ".test." not in name and ".spec." not in name:
            continue
        rel_path = path.relative_to(repo).as_posix()
        if not is_test_path(rel_path):
            continue
        rel = f"{repo.name}/{rel_path}"
        try:
            out.append((rel, path.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return out


def _has_spring_test_dep(repo: Path) -> bool:
    for name in ("pom.xml", "build.gradle", "build.gradle.kts"):
        path = repo / name
        if path.is_file() and _SPRING_TEST_DEP.search(_read_text(path)):
            return True
    return False


def _java_test_files(repo: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for path in repo.rglob("*Test.java"):
        rel = str(path.relative_to(repo)).replace("\\", "/")
        if "src/test/" not in rel or "/target/" in rel or "/build/" in rel:
            continue
        if path.is_file():
            out.append((rel, _read_text(path)))
    return out


def _is_coverage_padding(rel: str, text: str) -> bool:
    stem = Path(rel).stem
    if _COVERAGE_TEST.search(stem) or _COVERAGE_TEST.search(text[:800]):
        pass
    elif not re.search(r"coverage|jacoco|getter|setter", text[:600], re.IGNORECASE):
        return False
    else:
        if not (_COVERAGE_TEST.search(stem) or "coverage" in stem.lower()):
            return False
    if _WIRING_ASSERT.search(text):
        return False
    if _GETTER_SETTER_ASSERT.search(text):
        return True
    if re.search(r"assertNotNull\(|assertEquals\(.*null", text) and not re.search(
        r"mockMvc|RestAssured|TestRestTemplate|@Autowired", text, re.IGNORECASE
    ):
        return True
    return False


def _is_shallow_stub(text: str) -> bool:
    if _MARKER not in text:
        return False
    if _BEHAVIOR_ASSERT.search(text):
        return False
    return bool(_SYMBOL_ONLY.search(text))


def _boilerplate_key(text: str) -> tuple[str, str] | None:
    mod_m = _IMPORT_MOD.search(text)
    sym_m = _CLASS_SYMBOL.search(text)
    if not mod_m or not sym_m:
        return None
    block = text
    if _PER_FILE_PATH_HACK.search(text):
        block = _IMPORT_MOD.sub("", text)
        block = _PER_FILE_PATH_HACK.sub("", block)
    return mod_m.group(1), sym_m.group(1)


class TestQualitySensor:
    id = "test_quality"
    goal = AnalysisGoal.COVERAGE

    def available(self, ctx: AnalysisContext) -> bool:
        return bool(ctx.repos)

    def collect(self, ctx: AnalysisContext) -> list[Finding]:
        known = ctx.known_ids()
        findings: list[Finding] = []
        shallow: list[str] = []
        boiler_groups: dict[tuple[str, str, str], list[str]] = defaultdict(list)

        for repo in ctx.repos:
            for rel, text in _scan_repo_tests(repo):
                if _is_shallow_stub(text):
                    shallow.append(rel)
                key = _boilerplate_key(text)
                if key and _PER_FILE_PATH_HACK.search(text):
                    mod, sym = key
                    boiler_groups[(repo.name, mod, sym)].append(rel)

            if _has_spring_test_dep(repo):
                java_tests = _java_test_files(repo)
                test_classes = [rel for rel, text in java_tests if _JAVA_TEST_CLASS.search(text)]
                all_text = "\n".join(t for _, t in java_tests)
                has_integration = bool(_INTEGRATION_ANNOT.search(all_text))
                uid = _repo_unit(ctx, repo.name)
                if uid and len(test_classes) >= 8 and not has_integration:
                    f = try_make_finding(
                        id=content_hash(f"test_quality:java_mock_only:{repo.name}"),
                        goal=self.goal,
                        sensor=self.id,
                        severity=Severity.HIGH,
                        title=f"Mock-only Java test suite in `{repo.name}` ({len(test_classes)} classes)",
                        evidence=(
                            f"{len(test_classes)} src/test classes with spring-boot-starter-test on classpath "
                            f"but zero @SpringBootTest/@DataMongoTest/@EmbeddedKafka/Testcontainers — "
                            "WebMvcTest/Mockito smoke only; add integration tests before refactors."
                        ),
                        unit_ids=[uid],
                        action_hint=SuggestedAction.ENSURE_TESTS,
                        score=0.9,
                        metadata={
                            "kind": "java_mock_only",
                            "repo": repo.name,
                            "test_class_count": len(test_classes),
                        },
                        known_ids=known,
                    )
                    if f:
                        findings.append(f)

                for rel, text in java_tests:
                    if not _is_coverage_padding(rel, text):
                        continue
                    uid = _unit_id_for_test(ctx, f"{repo.name}/{rel}") or _repo_unit(ctx, repo.name)
                    if not uid:
                        continue
                    f = try_make_finding(
                        id=content_hash(f"test_quality:coverage_padding:{repo.name}:{rel}"),
                        goal=self.goal,
                        sensor=self.id,
                        severity=Severity.MEDIUM,
                        title=f"Coverage-padding test `{Path(rel).name}` in {repo.name}",
                        evidence=(
                            f"`{rel}` appears written for Jacoco/coverage metrics with getter/setter smoke "
                            "asserts only — no HTTP/Mongo/Kafka wiring assertions."
                        ),
                        unit_ids=[uid],
                        action_hint=SuggestedAction.ENSURE_TESTS,
                        score=0.78,
                        metadata={"kind": "coverage_padding", "repo": repo.name, "test_path": rel},
                        known_ids=known,
                    )
                    if f:
                        findings.append(f)

        for rel in shallow:
            uid = _unit_id_for_test(ctx, rel)
            if not uid:
                continue
            f = try_make_finding(
                id=content_hash(f"test_quality:shallow:{rel}"),
                goal=self.goal,
                sensor=self.id,
                severity=Severity.HIGH,
                title=f"Shallow autorefactor test `{Path(rel).name}`",
                evidence=(
                    "Test only asserts importability/symbol existence (autorefactor ensure-tests) "
                    "with no behavioral assertions — filename presence is not coverage."
                ),
                unit_ids=[uid],
                action_hint=SuggestedAction.ENSURE_TESTS,
                score=0.85,
                metadata={"test_path": rel, "kind": "symbol_exists_only"},
                known_ids=known,
            )
            if f:
                findings.append(f)

        for (repo_name, mod, sym), paths in boiler_groups.items():
            if len(paths) < 2:
                continue
            uids = [u for p in paths if (u := _unit_id_for_test(ctx, p))]
            if not uids:
                continue
            f = try_make_finding(
                id=content_hash(f"test_quality:boiler:{repo_name}:{mod}:{sym}"),
                goal=self.goal,
                sensor=self.id,
                severity=Severity.HIGH,
                title=f"Duplicate test boilerplate for `{sym}` ({len(paths)} files)",
                evidence=(
                    f"{len(paths)} test files repeat identical import/setup for "
                    f"`{mod}.{sym}` instead of one behavioral module — "
                    f"examples: {', '.join(Path(p).name for p in paths[:4])}."
                ),
                unit_ids=list(dict.fromkeys(uids))[:8],
                action_hint=SuggestedAction.ENSURE_TESTS,
                score=0.88,
                metadata={
                    "kind": "duplicate_boilerplate",
                    "repo": repo_name,
                    "module": mod,
                    "symbol": sym,
                    "test_paths": paths,
                },
                known_ids=known,
            )
            if f:
                findings.append(f)

        return findings


def test_quality() -> TestQualitySensor:
    return TestQualitySensor()
