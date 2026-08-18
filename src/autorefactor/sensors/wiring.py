from __future__ import annotations

import re
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
from autorefactor.sensors.anchors import production_type_for_repo, resolve_file_unit
from autorefactor.sensors.base import AnalysisContext

_KAFKA_DEP = re.compile(
    r"spring-kafka|org\.apache\.kafka|kafka-clients",
    re.IGNORECASE,
)
_KAFKA_CONFIG = re.compile(
    r"(?m)^\s*(spring\.kafka|psn\.|kafka\.)|bootstrap-servers|consumer\.group|listener\.ack",
    re.IGNORECASE,
)
_KAFKA_JAVA_CONFIG = re.compile(
    r"@(?:Value|ConfigurationProperties)\s*\(\s*[\"']\$\{(?:psn\.|spring\.kafka\.|kafka\.)"
    r"|@ConfigurationProperties\s*\(\s*[\"'](?:psn|kafka|spring\.kafka)",
    re.IGNORECASE,
)
_DISABLED_VALIDATOR = re.compile(
    r"//\s*@PostConstruct[\s\S]{0,400}?\bvalidate\w*Configuration\s*\(",
    re.IGNORECASE,
)
_KAFKA_LISTENER = re.compile(r"@KafkaListener\b|KafkaListener\b|ConsumerRecord\b")
_HANDLER_METHOD = re.compile(
    r"(?:public|protected)\s+(?:\w+\s+)*(\w+)\s*\([^)]*(?:ConsumerRecord|process\w*Transaction)",
    re.MULTILINE,
)
_PROCESS_TXN = re.compile(r"\bprocess\w*Transaction\s*\(")
_CONSUMER_RECORD_PARAM = re.compile(r"ConsumerRecord\s*<")
_FIND_BY = re.compile(r"\bfindBy\w+\s*\(")
_ALWAYS_WRITE = re.compile(
    r"\b(?:increment)\s*\(|balance_\$\{|balance_\"\s*\+|epochMillis|currentTimeMillis\s*\(",
    re.IGNORECASE,
)
_INGESTION_METHOD = re.compile(
    r"\b(process\w+|handle\w+|consume\w+)\s*\(",
    re.MULTILINE,
)
_GRPC_TYPE = re.compile(
    r"@GrpcService\b|implements\s+BindableService|"
    r"\bclass\s+\w+GrpcService\b|extends\s+\w+(?:Grpc\.\w+)?ImplBase\b",
    re.IGNORECASE,
)
_BIT_PATH = re.compile(r"\bin\s+(\S+\.\w+)\b")


def _read_text(path: Path, limit: int = 500_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _is_build_artifact(rel: str) -> bool:
    return rel.startswith(("target/", "build/")) or "/target/" in rel or "/build/" in rel


def _java_sources(repo: Path, *, test: bool) -> list[Path]:
    out: list[Path] = []
    for path in repo.rglob("*.java"):
        rel = str(path.relative_to(repo)).replace("\\", "/")
        if _is_build_artifact(rel):
            continue
        in_test = "/src/test/" in rel
        if test and not in_test:
            continue
        if not test and in_test:
            continue
        out.append(path)
    return out


def _scan_repo(repo: Path) -> tuple[bool, bool, bool, list[str]]:
    """Return (has_dep, has_config, has_listener, evidence_bits)."""
    has_dep = False
    has_config = False
    has_listener = False
    bits: list[str] = []

    for name in ("pom.xml", "build.gradle", "build.gradle.kts"):
        path = repo / name
        if path.is_file() and _KAFKA_DEP.search(_read_text(path)):
            has_dep = True
            bits.append(f"dependency in {name}")
            break

    for pattern in ("**/application*.yml", "**/application*.yaml", "**/application*.properties"):
        for path in repo.glob(pattern):
            if not path.is_file():
                continue
            rel = str(path.relative_to(repo)).replace("\\", "/")
            if _is_build_artifact(rel):
                continue
            text = _read_text(path)
            if _KAFKA_DEP.search(text) or _KAFKA_CONFIG.search(text):
                has_config = True
                bits.append(f"config in {rel}")
                break
        if has_config:
            break

    for path in _java_sources(repo, test=False):
        rel = str(path.relative_to(repo)).replace("\\", "/")
        text = _read_text(path)
        if _KAFKA_LISTENER.search(text):
            has_listener = True
            bits.append(f"listener in {rel}")
            break
        if _KAFKA_JAVA_CONFIG.search(text):
            has_config = True
            bits.append(f"kafka/psn keys via @Value/@ConfigurationProperties in {rel}")
        if _DISABLED_VALIDATOR.search(text):
            has_config = True
            bits.append(f"disabled validateConfiguration (@PostConstruct commented) in {rel}")

    handlers = _handler_methods_in_main(repo)
    if handlers and not has_listener:
        rel, cls, method = handlers[0]
        has_config = True
        bits.append(f"process handler `{cls}.{method}` in {rel} without @KafkaListener")

    return has_dep, has_config, has_listener, bits


def _handler_methods_in_main(repo: Path) -> list[tuple[str, str, str]]:
    """Return (rel_path, class_name, method_name) for candidate Kafka handlers."""
    handlers: list[tuple[str, str, str]] = []
    for path in _java_sources(repo, test=False):
        text = _read_text(path)
        if not (_PROCESS_TXN.search(text) or _CONSUMER_RECORD_PARAM.search(text)):
            continue
        rel = str(path.relative_to(repo)).replace("\\", "/")
        class_m = re.search(r"class\s+(\w+)", text)
        class_name = class_m.group(1) if class_m else path.stem
        for m in re.finditer(
            r"(?:public|protected)\s+(?:\w[\w<>,\s]*\s+)?(\w+)\s*\([^)]*\)\s*(?:throws\s+[\w,\s]+)?\s*\{",
            text,
        ):
            name = m.group(1)
            if name in {"class", "interface", "enum", "if", "for", "while"}:
                continue
            start = m.start()
            sig = text[max(0, start - 80) : m.end()]
            if re.match(r"process\w*Transaction", name) or _CONSUMER_RECORD_PARAM.search(sig):
                handlers.append((rel, class_name, name))
    return handlers


def _symbol_references(repo: Path, symbol: str, defining_rel: str) -> tuple[bool, bool]:
    """Return (referenced_in_main_elsewhere, referenced_in_test)."""
    main_hit = False
    test_hit = False
    pat = re.compile(rf"\b{re.escape(symbol)}\s*\(")
    for path in repo.rglob("*.java"):
        rel = str(path.relative_to(repo)).replace("\\", "/")
        if _is_build_artifact(rel):
            continue
        text = _read_text(path)
        if not pat.search(text):
            continue
        if "src/test/" in rel:
            test_hit = True
        elif rel != defining_rel:
            main_hit = True
    return main_hit, test_hit


def _test_only_handlers(repo: Path) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for rel, cls, method in _handler_methods_in_main(repo):
        main_ref, test_ref = _symbol_references(repo, method, rel)
        if test_ref and not main_ref:
            out.append((rel, cls, method))
    return out


def _type_ingestion_methods(text: str) -> list[tuple[str, bool, bool]]:
    """Return (method_name, has_find_by, has_always_write) per public method body."""
    out: list[tuple[str, bool, bool]] = []
    for m in re.finditer(
        r"(?:public|protected)\s+(?:\w[\w<>,\s]*\s+)?(\w+)\s*\([^)]*\)\s*(?:throws\s+[\w,\s]+)?\s*\{",
        text,
    ):
        name = m.group(1)
        if not re.match(r"(process|handle|consume)", name, re.IGNORECASE):
            continue
        start = m.end()
        depth = 1
        i = start
        while i < len(text) and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        body = text[start : i - 1] if i > start else ""
        out.append((name, bool(_FIND_BY.search(body)), bool(_ALWAYS_WRITE.search(body))))
    return out


def _asymmetric_dedupe(repo: Path) -> list[tuple[str, str, str, str]]:
    """Return (rel, type_name, dedupe_method, always_write_method)."""
    pairs: list[tuple[str, str, str, str]] = []
    for path in _java_sources(repo, test=False):
        text = _read_text(path)
        if not _INGESTION_METHOD.search(text):
            continue
        rel = str(path.relative_to(repo)).replace("\\", "/")
        class_m = re.search(r"class\s+(\w+)", text)
        type_name = class_m.group(1) if class_m else path.stem
        methods = _type_ingestion_methods(text)
        dedupe = [n for n, fb, aw in methods if fb and not aw]
        always = [n for n, fb, aw in methods if aw and not fb]
        if dedupe and always:
            pairs.append((rel, type_name, dedupe[0], always[0]))
    return pairs


def _rel_from_bits(bits: list[str]) -> str | None:
    for bit in bits:
        m = _BIT_PATH.search(bit)
        if m:
            return m.group(1).rstrip(".,;")
    return None


def _anchor_unit(ctx: AnalysisContext, repo: str, rel: str | None):
    if rel:
        unit = resolve_file_unit(ctx, repo, rel)
        if unit is not None:
            return unit
    return production_type_for_repo(ctx, repo)


def _grpc_entrypoints(repo: Path) -> list[tuple[str, str]]:
    """Return (rel_path, class_name) for gRPC service types in src/main."""
    out: list[tuple[str, str]] = []
    for path in _java_sources(repo, test=False):
        text = _read_text(path)
        if not _GRPC_TYPE.search(text):
            continue
        rel = str(path.relative_to(repo)).replace("\\", "/")
        class_m = re.search(r"class\s+(\w+)", text)
        out.append((rel, class_m.group(1) if class_m else path.stem))
    return out


def _has_grpc_characterization(repo: Path, class_name: str) -> bool:
    needle = class_name.lower().replace("grpcservice", "").replace("service", "")
    for path in _java_sources(repo, test=True):
        name = path.name.lower()
        if "grpc" in name or (needle and needle in name):
            return True
    return False


class WiringSensor:
    """Detect declared messaging/config wiring with no runtime consumer."""

    id = "wiring"
    goal = AnalysisGoal.STRUCTURE

    def available(self, ctx: AnalysisContext) -> bool:
        return any((repo / "pom.xml").exists() or (repo / "build.gradle").exists() for repo in ctx.repos)

    def collect(self, ctx: AnalysisContext) -> list[Finding]:
        known = ctx.known_ids()
        findings: list[Finding] = []

        for repo_path in ctx.repos:
            repo_name = repo_path.name
            has_dep, has_config, has_listener, bits = _scan_repo(repo_path)
            if has_dep or has_config:
                if not has_listener:
                    rel = _rel_from_bits(bits)
                    unit = _anchor_unit(ctx, repo_name, rel)
                    if unit is not None:
                        evidence = (
                            "Kafka/PSN wiring present without @KafkaListener consumer "
                            f"({'; '.join(bits)}). "
                            "Architectural decision: wire_kafka_consumer (add @KafkaListener "
                            "for the process*Transaction handler) or remove_dead_wiring "
                            "(drop orphan handler and PSN config in application.yml)."
                        )
                        f = try_make_finding(
                            id=content_hash(f"wiring:kafka:{repo_name}"),
                            goal=self.goal,
                            sensor=self.id,
                            severity=Severity.HIGH,
                            title=f"Orphan Kafka/messaging wiring in {repo_name}",
                            evidence=evidence,
                            unit_ids=[unit.id],
                            action_hint=SuggestedAction.DOCUMENT,
                            score=0.9,
                            hints=["wire_kafka_consumer", "remove_dead_wiring"],
                            metrics={
                                "has_dep": 1.0 if has_dep else 0.0,
                                "has_config": 1.0 if has_config else 0.0,
                            },
                            metadata={
                                "kind": "orphan_kafka",
                                "repo": repo_name,
                                "path": unit.path,
                            },
                            known_ids=known,
                        )
                        if f:
                            findings.append(f)

                    for rel, cls, method in _test_only_handlers(repo_path):
                        handler = _anchor_unit(ctx, repo_name, rel)
                        if handler is None:
                            continue
                        f = try_make_finding(
                            id=content_hash(f"wiring:test_handler:{repo_name}:{rel}:{method}"),
                            goal=self.goal,
                            sensor=self.id,
                            severity=Severity.MEDIUM,
                            title=f"Test-only Kafka handler `{cls}.{method}` in {repo_name}",
                            evidence=(
                                f"`{rel}` defines `{cls}.{method}` (process*Transaction / ConsumerRecord) "
                                "but references appear only under src/test. "
                                "Architectural decision: wire_kafka_consumer (add @KafkaListener) "
                                "or remove_dead_wiring (delete dead handler and orphan PSN config)."
                            ),
                            unit_ids=[handler.id],
                            action_hint=SuggestedAction.DOCUMENT,
                            score=0.88,
                            hints=["wire_kafka_consumer", "remove_dead_wiring"],
                            metadata={
                                "kind": "test_only_handler",
                                "repo": repo_name,
                                "path": rel,
                                "method": method,
                            },
                            known_ids=known,
                        )
                        if f:
                            findings.append(f)

                    for rel, type_name, dedupe_m, always_m in _asymmetric_dedupe(repo_path):
                        dedupe_unit = _anchor_unit(ctx, repo_name, rel)
                        if dedupe_unit is None:
                            continue
                        f = try_make_finding(
                            id=content_hash(
                                f"wiring:asymmetric_dedupe:{repo_name}:{rel}:{dedupe_m}:{always_m}"
                            ),
                            goal=self.goal,
                            sensor=self.id,
                            severity=Severity.MEDIUM,
                            title=f"Asymmetric ingestion dedupe in `{type_name}` ({repo_name})",
                            evidence=(
                                f"`{rel}` — `{dedupe_m}` dedupes via findBy* before write while "
                                f"`{always_m}` always increments/writes without lookup. Align idempotency strategy."
                            ),
                            unit_ids=[dedupe_unit.id],
                            action_hint=SuggestedAction.DOCUMENT,
                            score=0.8,
                            metadata={
                                "kind": "asymmetric_dedupe",
                                "repo": repo_name,
                                "path": rel,
                                "dedupe_method": dedupe_m,
                                "always_write_method": always_m,
                            },
                            known_ids=known,
                        )
                        if f:
                            findings.append(f)

            for rel, cls in _grpc_entrypoints(repo_path):
                if _has_grpc_characterization(repo_path, cls):
                    continue
                grpc_unit = _anchor_unit(ctx, repo_name, rel)
                if grpc_unit is None:
                    continue
                f = try_make_finding(
                    id=content_hash(f"wiring:grpc:{repo_name}:{rel}"),
                    goal=self.goal,
                    sensor=self.id,
                    severity=Severity.MEDIUM,
                    title=f"gRPC entry `{cls}` lacks characterization tests",
                    evidence=(
                        f"`{rel}` exposes `{cls}` (@GrpcService / BindableService / *GrpcService) "
                        "with no matching src/test characterization — queue ensure_tests before refactors."
                    ),
                    unit_ids=[grpc_unit.id],
                    action_hint=SuggestedAction.ENSURE_TESTS,
                    score=0.83,
                    metadata={"kind": "grpc_entry", "repo": repo_name, "path": rel, "symbol": cls},
                    known_ids=known,
                )
                if f:
                    findings.append(f)

        return findings

