from __future__ import annotations

from pathlib import Path

from autorefactor.enrich.coverage_parsers import load_coverage_snapshot


def _write_jacoco(path: Path, classname: str, covered: int, missed: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<report name="demo">
  <package name="com/example">
    <sourcefile name="{classname}">
      <counter type="LINE" missed="{missed}" covered="{covered}"/>
    </sourcefile>
  </package>
  <counter type="LINE" missed="{missed}" covered="{covered}"/>
</report>
""",
        encoding="utf-8",
    )


def test_parse_jacoco_xml_with_namespace(tmp_path: Path) -> None:
    """Real JaCoCo reports use xmlns; parser must still index source files."""
    jacoco = tmp_path / "jacoco.xml"
    jacoco.write_text(
        """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<report xmlns="http://www.jacoco.org/dtd/report" name="demo">
  <package name="com/example">
    <sourcefile name="ConfigurationValidator.java">
      <counter type="LINE" missed="3" covered="7"/>
    </sourcefile>
  </package>
  <counter type="LINE" missed="3" covered="7"/>
</report>""",
        encoding="utf-8",
    )
    from autorefactor.enrich.coverage_parsers import parse_jacoco_xml

    snap = parse_jacoco_xml(jacoco)
    assert len(snap.files) == 1
    assert snap.files[0].path.endswith("ConfigurationValidator.java")
    assert snap.files[0].covered_lines == 7


def test_nested_maven_aggregator_jacoco_discovered(tmp_path: Path) -> None:
    """JaCoCo at aggregator target/ is indexed when repos are nested modules only."""
    from autorefactor.enrich.coverage_parsers import load_coverage_snapshot

    parent = tmp_path / "janus-bonus"
    module = parent / "service-module"
    module.mkdir(parents=True)
    (parent / "pom.xml").write_text(
        "<project><modules><module>service-module</module></modules></project>",
        encoding="utf-8",
    )
    (module / "pom.xml").write_text("<project/>", encoding="utf-8")
    jacoco = parent / "target" / "site" / "jacoco" / "jacoco.xml"
    jacoco.parent.mkdir(parents=True)
    jacoco.write_text(
        """<?xml version="1.0"?>
<report><package name="com/example"><sourcefile name="Svc.java">
<counter type="LINE" missed="3" covered="7"/></sourcefile></package>
<counter type="LINE" missed="3" covered="7"/></report>""",
        encoding="utf-8",
    )
    snap = load_coverage_snapshot(parent, [module])
    assert snap is not None
    assert len(snap.files) > 0
    assert snap.source == "jacoco"


def test_merge_two_repo_jacoco(tmp_path: Path) -> None:
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    _write_jacoco(alpha / "target/site/jacoco/jacoco.xml", "A.java", covered=8, missed=2)
    _write_jacoco(beta / "target/site/jacoco/jacoco.xml", "B.java", covered=1, missed=9)
    snap = load_coverage_snapshot(tmp_path, [alpha, beta])
    assert snap is not None
    paths = {f.path for f in snap.files}
    assert any(p.endswith("A.java") and p.startswith("alpha/") for p in paths)
    assert any(p.endswith("B.java") and p.startswith("beta/") for p in paths)
    assert snap.repos_without_coverage == []


def test_repos_without_coverage_partial(tmp_path: Path) -> None:
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    beta.mkdir()
    _write_jacoco(alpha / "target/site/jacoco/jacoco.xml", "A.java", covered=5, missed=5)
    snap = load_coverage_snapshot(tmp_path, [alpha, beta])
    assert snap is not None
    assert "beta" in snap.repos_without_coverage
    assert "alpha" not in snap.repos_without_coverage


def test_maven_target_list_coverage_json_does_not_crash(tmp_path: Path) -> None:
    """Non-coverage JSON lists under target/**/coverage.json must be ignored."""
    repo = tmp_path / "svc"
    bad = repo / "target" / "foo" / "coverage.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("[]", encoding="utf-8")
    _write_jacoco(repo / "target/site/jacoco/jacoco.xml", "Ok.java", covered=4, missed=1)
    snap = load_coverage_snapshot(tmp_path, [repo])
    assert snap is not None
    assert any(f.path.endswith("Ok.java") for f in snap.files)


def test_root_list_shaped_coverage_json_does_not_crash(tmp_path: Path) -> None:
    """Malformed coverage-like JSON at repo root must not crash load."""
    repo = tmp_path / "svc"
    repo.mkdir()
    (repo / "coverage.json").write_text('{"files": []}', encoding="utf-8")
    (repo / "other.json").write_text('{"files": [{"path": "x"}]}', encoding="utf-8")
    snap = load_coverage_snapshot(tmp_path, [repo])
    # May be missing or empty — must not raise
    assert snap is None or isinstance(snap.files, list)


def test_enrich_coverage_never_raises(tmp_path: Path, monkeypatch) -> None:
    from autorefactor.sensors.coverage import enrich_coverage

    def boom(*_a, **_k):  # noqa: ANN001
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(
        "autorefactor.enrich.coverage_parsers.load_coverage_snapshot",
        boom,
    )
    repo = tmp_path / "svc"
    repo.mkdir()
    snap = enrich_coverage(workspace=tmp_path, repos=[repo])
    assert snap.source == "missing"
    assert snap.files == []
