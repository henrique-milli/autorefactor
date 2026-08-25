from __future__ import annotations

from pathlib import Path

from autorefactor.config import AutorefactorConfig
from autorefactor.domain import CodeSpan, CodeUnit, UnitKind, content_hash
from autorefactor.sensors.base import AnalysisContext
from autorefactor.sensors.fingerprint import FingerprintCloneSensor
from autorefactor.sensors.slop import SlopSensor


def _fn(uid: str, repo: str, path: str, name: str, body: str) -> CodeUnit:
    return CodeUnit(
        id=uid,
        repo=repo,
        path=path,
        qualified_name=name,
        kind=UnitKind.FUNCTION,
        language="java",
        source_snippet=body,
        content_hash=content_hash(body),
        span=CodeSpan(start_line=1, end_line=body.count("\n") + 1),
    )


_BODY_A = """
public int computeTotal(List<Order> orders) {
    int sum = 0;
    for (Order order : orders) {
        if (order.isActive()) {
            sum += order.getAmount();
            sum += order.getTax();
        }
    }
    return sum;
}
"""

_BODY_B = """
public int tallyAmounts(List<Purchase> purchases) {
    int acc = 0;
    for (Purchase purchase : purchases) {
        if (purchase.isActive()) {
            acc += purchase.getAmount();
            acc += purchase.getTax();
        }
    }
    return acc;
}
"""


def test_fingerprint_sensor_finds_renamed_clone(tmp_path: Path) -> None:
    units = [
        _fn("a", "alpha", "svc/A.java", "A.computeTotal", _BODY_A),
        _fn("b", "beta", "svc/B.java", "B.tallyAmounts", _BODY_B),
    ]
    ctx = AnalysisContext(
        workspace=tmp_path,
        repos=(tmp_path / "alpha", tmp_path / "beta"),
        units=tuple(units),
        config=AutorefactorConfig(workspace=tmp_path, skip_llm=True),
        focus="clones",
    )
    findings = FingerprintCloneSensor().collect(ctx)
    assert findings
    assert any(f.sensor == "sim.fingerprint" for f in findings)


def test_identity_helpers_python_nested_def_no_redos() -> None:
    """Nested defs must not ReDoS the identity scanner (cross-repo smoke hang)."""
    from autorefactor.sensors.slop import _identity_helpers_in_source

    body = (
        "def main() -> None:\n"
        "    calls = {\"n\": 0}\n"
        "\n"
        "    def fake_urlopen(req, timeout=180):\n"
        "        calls[\"n\"] += 1\n"
        "        if calls[\"n\"] == 1:\n"
        "            raise RuntimeError(\"retry\")\n"
        "        return b\"ok\"\n"
        "\n"
        "    assert calls[\"n\"] == 0\n"
        "    print(\"done\")\n"
    )
    # Pad to amplify backtracking on the old unbounded line-loop pattern.
    body = body + ("    x = 1\n" * 200)
    assert _identity_helpers_in_source(body, "python") == []
    # Still detects a real short identity helper.
    id_body = "def noop(x):\n    return x\n"
    assert _identity_helpers_in_source(id_body, "python") == [("noop", "x")]


def test_slop_identity_helper_go(tmp_path: Path) -> None:
    body = '''package util

func NormalizeUserID(raw string) string {
    value := stringsTrimLower(raw)
    return replaceSpaces(value)
}

func stringsTrimLower(s string) string {
    return s
}
'''
    units = [
        CodeUnit(
            id="go-n",
            repo="alpha",
            path="util/normalize.go",
            qualified_name="NormalizeUserID",
            kind=UnitKind.FUNCTION,
            language="go",
            source_snippet=body,
            content_hash=content_hash(body),
            span=CodeSpan(start_line=1, end_line=12),
        ),
    ]
    ctx = AnalysisContext(
        workspace=tmp_path,
        repos=(tmp_path / "alpha",),
        units=tuple(units),
        config=AutorefactorConfig(workspace=tmp_path, skip_llm=True),
        focus="slop",
    )
    findings = SlopSensor().collect(ctx)
    assert any("stringsTrimLower" in f.title or "stringsTrimLower" in f.evidence for f in findings)


def test_slop_skips_same_file_config_validators(tmp_path: Path) -> None:
    """Sibling validatePam* / validatePsn* if-chains are not delete_duplicate fodder."""
    path = "src/main/java/com/example/config/ConfigurationValidator.java"
    pam = _fn(
        "pam",
        "active",
        path,
        "ConfigurationValidator.validatePamConfiguration",
        """
private void validatePamConfiguration(StringBuilder errors) {
    if (!StringUtils.hasText(pamBaseUrl)) {
        errors.append("PAM base-url is not configured. ");
    }
    if (!StringUtils.hasText(pamClientId)) {
        errors.append("PAM client-id is not configured. ");
    }
    if (!StringUtils.hasText(pamHmacSecret)) {
        errors.append("PAM hmac-secret is not configured. ");
    }
}
""",
    )
    psn = _fn(
        "psn",
        "active",
        path,
        "ConfigurationValidator.validatePsnConfiguration",
        """
private void validatePsnConfiguration(StringBuilder errors) {
    if (!StringUtils.hasText(psnBootstrapServer)) {
        errors.append("Pokerstars Kafka bootstrap.server is not configured. ");
    }
    if (!StringUtils.hasText(psnTransactionTopic)) {
        errors.append("Pokerstars Kafka topics.transaction is not configured. ");
    }
    if (!StringUtils.hasText(psnConsumerGroupId)) {
        errors.append("Pokerstars Kafka consumer.group-id is not configured. ");
    }
    if (psnSecurityEnabled && !StringUtils.hasText(psnJaasConfig)) {
        errors.append("Pokerstars Kafka security.sasl.jaas.config is not configured. ");
    }
}
""",
    )
    ctx = AnalysisContext(
        workspace=tmp_path,
        repos=(tmp_path / "active",),
        units=(pam, psn),
        config=AutorefactorConfig(workspace=tmp_path, skip_llm=True),
        focus="slop",
    )
    findings = SlopSensor().collect(ctx)
    clusters = [
        f
        for f in findings
        if (f.metadata or {}).get("kind") == "slop_cluster"
        or f.title.startswith("Slop cluster")
    ]
    assert clusters == []
    assert not any(f.action_hint.value == "delete_duplicate" for f in findings)


def test_slop_sensor_emits_score(tmp_path: Path) -> None:
    units = [
        _fn("a", "alpha", "svc/A.java", "A.computeTotal", _BODY_A),
        _fn("b", "beta", "svc/B.java", "B.tallyAmounts", _BODY_B),
        _fn("c", "gamma", "svc/C.java", "C.computeTotal2", _BODY_A.replace("computeTotal", "computeTotal2")),
        _fn("d", "delta", "svc/D.java", "D.other", _BODY_B.replace("tallyAmounts", "other")),
    ]
    ctx = AnalysisContext(
        workspace=tmp_path,
        repos=tuple(tmp_path / r for r in ("alpha", "beta", "gamma", "delta")),
        units=tuple(units),
        config=AutorefactorConfig(workspace=tmp_path, skip_llm=True),
        focus="slop",
    )
    findings = SlopSensor().collect(ctx)
    assert findings
    assert any((f.metrics or {}).get("slop_score", 0) > 0 for f in findings)


def test_draft_map_dedupes_type_and_file_scan(tmp_path: Path) -> None:
    """TYPE + file rglob must emit one finding; never attach to REPO path '.'."""
    repo = tmp_path / "janus-bonus"
    java_rel = "src/main/java/com/example/PokerstarsProductMappingService.java"
    java_path = repo / java_rel
    java_path.parent.mkdir(parents=True)
    src = (
        "// TODO draft product mapping\n"
        "public class PokerstarsProductMappingService {\n"
        "  private final Map<String, String> defaultMappings = new HashMap<>();\n"
        "}\n"
    )
    java_path.write_text(src, encoding="utf-8")
    type_unit = CodeUnit(
        id="u-type",
        repo="janus-bonus",
        path=java_rel,
        language="java",
        kind=UnitKind.TYPE,
        qualified_name="com.example.PokerstarsProductMappingService",
        source_snippet=src,
        content_hash=content_hash(src),
    )
    repo_unit = CodeUnit(
        id="u-repo",
        repo="janus-bonus",
        path=".",
        language="java",
        kind=UnitKind.REPO,
        qualified_name="janus-bonus",
        source_snippet="",
        content_hash=content_hash("repo"),
    )
    ctx = AnalysisContext(
        workspace=tmp_path,
        repos=(repo,),
        units=(type_unit, repo_unit),
        config=AutorefactorConfig(workspace=tmp_path, skip_llm=True),
        focus="slop",
    )
    findings = SlopSensor().collect(ctx)
    draft = [f for f in findings if (f.metadata or {}).get("kind") == "draft_in_memory_map"]
    assert len(draft) == 1
    assert draft[0].unit_ids == ["u-type"]
    assert (draft[0].metadata or {}).get("path") != "."


def test_disabled_validation_without_readme(tmp_path: Path) -> None:
    """Commented @PostConstruct on config validators surfaces without README gate."""
    repo = tmp_path / "janus-bonus"
    java_rel = "src/main/java/com/example/config/ConfigurationValidator.java"
    java_path = repo / java_rel
    java_path.parent.mkdir(parents=True)
    src = (
        "package com.example.config;\n"
        "import javax.annotation.PostConstruct;\n"
        "public class ConfigurationValidator {\n"
        "  // @PostConstruct\n"
        "  public void validateConfiguration() {\n"
        "    // TODO PAM/PSN required properties\n"
        "  }\n"
        "}\n"
    )
    java_path.write_text(src, encoding="utf-8")
    type_unit = CodeUnit(
        id="u-validator",
        repo="janus-bonus",
        path=java_rel,
        language="java",
        kind=UnitKind.TYPE,
        qualified_name="com.example.config.ConfigurationValidator",
        source_snippet=src,
        content_hash=content_hash(src),
    )
    ctx = AnalysisContext(
        workspace=tmp_path,
        repos=(repo,),
        units=(type_unit,),
        config=AutorefactorConfig(workspace=tmp_path, skip_llm=True),
        focus="slop",
    )
    findings = SlopSensor().collect(ctx)
    disabled = [f for f in findings if (f.metadata or {}).get("kind") == "disabled_startup_validation"]
    assert len(disabled) == 1
    assert "validateConfiguration" in disabled[0].evidence
    assert "re-enable" in disabled[0].evidence.lower()
