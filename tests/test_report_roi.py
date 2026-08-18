"""Tests for report ROI scores, markdown structure, and visual smoke."""

from __future__ import annotations

import json
from pathlib import Path

from autorefactor.changesets.models import Changeset, ChangesetTarget
from autorefactor.domain import CodeSpan, CodeUnit, UnitKind, content_hash
from autorefactor.report.metrics import estimate_metrics, write_metrics_and_report
from autorefactor.report.roi import (
    agent_friction_score,
    composite_debt_score,
    compute_debt_components,
    per_task_agent_gains,
    value_score,
)
from autorefactor.report.visuals import generate_visuals, write_embedding_projection


def _unit(
    uid: str,
    *,
    repo: str = "alpha",
    kind: UnitKind = UnitKind.FUNCTION,
    embedding: list[float] | None = None,
    lines: int = 20,
) -> CodeUnit:
    snippet = "\n".join([f"line{i}" for i in range(lines)])
    return CodeUnit(
        id=uid,
        repo=repo,
        path=f"{uid}.py",
        qualified_name=f"mod.{uid}",
        kind=kind,
        language="python",
        source_snippet=snippet,
        content_hash=content_hash(uid),
        span=CodeSpan(start_line=1, end_line=lines),
        embedding=embedding,
    )


def test_debt_score_clone_heavy_above_empty() -> None:
    empty = compute_debt_components(
        split_cs=0,
        clone_paths=0,
        test_cs=0,
        doc_cs=0,
        slop_score=0.0,
        queue_n=0,
        findings_structure=0,
        findings_clone=0,
    )
    heavy = compute_debt_components(
        split_cs=2,
        clone_paths=4,
        test_cs=2,
        doc_cs=1,
        slop_score=40.0,
        queue_n=8,
        findings_structure=5,
        findings_clone=6,
    )
    assert composite_debt_score(heavy) > composite_debt_score(empty)
    assert agent_friction_score(
        debt_components=heavy, clone_paths=4, split_cs=2, test_cs=0
    ) > agent_friction_score(
        debt_components=empty, clone_paths=0, split_cs=0, test_cs=0
    )


def test_per_task_gains_ranks_multi_target_dedup_first() -> None:
    extract = Changeset(
        id="e1",
        title="Extract shared helper",
        action="extract_shared",
        brief="b",
        rationale="r",
        confidence=0.9,
        targets=[
            ChangesetTarget(repo="a", path="a.py", unit_id="u1"),
            ChangesetTarget(repo="b", path="b.py", unit_id="u2"),
        ],
        metrics_delta={"estimated_loc_removed": 40, "loc_under_review": 80},
    )
    vanity = Changeset(
        id="s1",
        title="Split god module",
        action="split",
        brief="b",
        rationale="r",
        confidence=0.9,
        targets=[ChangesetTarget(repo="a", path="big.py", unit_id="u3")],
        metrics_delta={"estimated_loc_removed": 0, "loc_under_review": 200},
    )
    gains = per_task_agent_gains([extract, vanity])
    assert gains["tasks"][0]["id"] == "e1"
    assert gains["tasks"][0]["retries_avoided"] > gains["tasks"][1]["retries_avoided"]
    assert gains["averages"]["tasks"] == 2


def test_value_score_extract_ranks_above_vanity_split() -> None:
    extract = Changeset(
        id="e1",
        title="Extract shared helper",
        action="extract_shared",
        brief="b",
        rationale="r",
        confidence=0.9,
        targets=[
            ChangesetTarget(repo="a", path="a.py", unit_id="u1"),
            ChangesetTarget(repo="b", path="b.py", unit_id="u2"),
        ],
        metrics_delta={"estimated_loc_removed": 40, "loc_under_review": 80},
    )
    vanity = Changeset(
        id="s1",
        title="Split god module",
        action="split",
        brief="b",
        rationale="r",
        confidence=0.9,
        targets=[ChangesetTarget(repo="a", path="big.py", unit_id="u3")],
        metrics_delta={"estimated_loc_removed": 0, "loc_under_review": 200},
    )
    vs_e = value_score(extract, max_rem=40.0, max_rev=200.0)
    vs_s = value_score(vanity, max_rem=40.0, max_rev=200.0)
    assert vs_e > vs_s


def test_estimate_metrics_includes_roi_block() -> None:
    units = [_unit("u1", lines=50), _unit("u2", repo="beta", lines=40)]
    extract = Changeset(
        id="c1",
        title="Extract shared",
        action="extract_shared",
        brief="b",
        rationale="r",
        confidence=0.92,
        targets=[
            ChangesetTarget(repo="alpha", path="u1.py", unit_id="u1"),
            ChangesetTarget(repo="beta", path="u2.py", unit_id="u2"),
        ],
        metrics_delta={"estimated_loc_removed": 15, "loc_under_review": 60, "goal": "method_similarity"},
    )
    split = Changeset(
        id="c2",
        title="Split module",
        action="split",
        brief="b",
        rationale="r",
        confidence=0.88,
        targets=[ChangesetTarget(repo="alpha", path="u1.py", unit_id="u1")],
        metrics_delta={"estimated_loc_removed": 0, "loc_under_review": 50, "goal": "structure"},
    )
    metrics = estimate_metrics(
        units=units, findings=[], confirmed=[], changesets=[extract, split]
    )
    roi = metrics["roi"]
    assert "debt_score" in roi
    assert roi["debt_score"]["label"] == "estimate"
    assert "components" in roi["debt_score"]
    assert "agent_friction" in roi
    assert roi["agent_friction"]["label"] == "estimate"
    assert "context_tokens_blast_radius" in roi
    assert "context_tokens_dedup_saved" in roi
    assert roi["context_tokens_dedup_saved"]["value"] == 15 * 4
    assert "attempt_savings_band" in roi
    assert "per_task_gains" in roi
    per = roi["per_task_gains"]
    assert per["averages"]["tasks"] == 2
    assert per["tasks"][0]["id"] == "c1"
    assert per["tasks"][0]["gain_tokens"] >= per["tasks"][1]["gain_tokens"]
    assert per["averages"]["gain_tokens"] > 0
    assert roi["value_ranked_changeset_ids"][0] == "c1"
    rows = metrics["upside_by_changeset"]
    assert rows[0]["id"] == "c1"
    assert "value_score" in rows[0]
    assert rows[0]["upside_kind"] == "dedup"
    assert rows[0]["plain_english"]
    assert metrics["scores"]["tech_debt"]["label"] == "estimate"


def test_report_md_roi_sections(tmp_path: Path) -> None:
    units = [_unit("u1", lines=100)]
    cs = Changeset(
        id="c1",
        title="Delete duplicate helper",
        action="delete_duplicate",
        brief="b",
        rationale="r",
        confidence=0.9,
        targets=[ChangesetTarget(repo="alpha", path="u1.py", unit_id="u1")],
        metrics_delta={"estimated_loc_removed": 12, "loc_under_review": 24, "goal": "method_similarity"},
    )
    metrics = estimate_metrics(units=units, findings=[], confirmed=[], changesets=[cs])
    report_dir = tmp_path / "report"
    write_metrics_and_report(report_dir, metrics=metrics, changesets=[cs], visual_paths=[])
    md = (report_dir / "report.md").read_text(encoding="utf-8")
    assert "## Executive brief" in md
    assert "## Agent economics" in md
    assert "## Technical debt snapshot" in md
    assert "## Top opportunities (by value score)" in md
    assert md.index("## Executive brief") < md.index("## Agent economics")
    assert md.index("## Agent economics") < md.index("## Technical debt snapshot")
    # No thrice-repeated upside dump / old detail list
    assert "Why refactor (the upside)" not in md
    assert "## Changesets (detail)" not in md
    assert md.count("## Executive brief") == 1
    assert "estimate" in md.lower()


def test_report_md_tiny_loc_leads_with_debt(tmp_path: Path) -> None:
    """Honesty rule: tiny removable LOC → executive brief leads with debt/friction."""
    units = [_unit("u1", lines=500)]
    cs = Changeset(
        id="c1",
        title="Split module",
        action="split",
        brief="b",
        rationale="r",
        confidence=0.8,
        targets=[ChangesetTarget(repo="alpha", path="u1.py", unit_id="u1")],
        metrics_delta={"estimated_loc_removed": 2, "loc_under_review": 200, "goal": "structure"},
    )
    metrics = estimate_metrics(units=units, findings=[], confirmed=[], changesets=[cs])
    report_dir = tmp_path / "report"
    write_metrics_and_report(report_dir, metrics=metrics, changesets=[cs], visual_paths=[])
    md = (report_dir / "report.md").read_text(encoding="utf-8")
    brief = md.split("## Agent economics")[0]
    assert "tiny" in brief.lower() or "debt/friction" in brief.lower()
    assert "Technical debt (estimate)" in brief
    assert "Agent friction (estimate)" in brief


def test_visual_smoke_tsne_clone_edges(tmp_path: Path) -> None:
    # Distinct enough embeddings for t-SNE with 5+ units
    rng = __import__("random").Random(0)
    units = []
    for i in range(8):
        emb = [rng.random() for _ in range(16)]
        emb[i % 16] += 2.0
        units.append(
            _unit(
                f"u{i}",
                kind=UnitKind.FUNCTION,
                embedding=emb,
                lines=10,
            )
        )
    cs = Changeset(
        id="pair",
        title="Extract twins",
        action="extract_shared",
        brief="b",
        rationale="r",
        confidence=0.9,
        targets=[
            ChangesetTarget(repo="alpha", path="u0.py", unit_id="u0"),
            ChangesetTarget(repo="alpha", path="u1.py", unit_id="u1"),
        ],
        metrics_delta={"estimated_loc_removed": 8, "loc_under_review": 20},
    )
    out = tmp_path / "visuals"
    path = write_embedding_projection(out / "embeddings_tsne.png", units, [cs])
    assert path is not None
    assert (out / "embeddings_tsne.png").is_file()

    metrics = estimate_metrics(units=units, findings=[], confirmed=[], changesets=[cs])
    report_dir = tmp_path / "report"
    paths = generate_visuals(
        report_dir / "visuals", units, [cs], metrics=metrics, report_dir=report_dir
    )
    names = {Path(p).name for p in paths}
    assert "embeddings_tsne.png" in names
    assert "debt_components.png" in names
    assert "upside_by_changeset.png" in names
    assert (report_dir / "projection.json").is_file()
    assert (report_dir / "graph.json").is_file()
    proj = json.loads((report_dir / "projection.json").read_text(encoding="utf-8"))
    assert proj["method"] in {"pca3", "tsne3"}
    assert proj["point_count"] >= 5
