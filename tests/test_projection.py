"""Tests for 3D projection + graph artifacts."""

from __future__ import annotations

import json
from pathlib import Path

from autorefactor.changesets.models import Changeset, ChangesetTarget
from autorefactor.domain import CodeSpan, CodeUnit, UnitKind, content_hash
from autorefactor.report.projection import (
    build_graph_payload,
    build_projection_payload,
    write_projection_artifacts,
)


def _unit(uid: str, emb: list[float]) -> CodeUnit:
    return CodeUnit(
        id=uid,
        repo="alpha",
        path=f"{uid}.py",
        qualified_name=f"mod.{uid}",
        kind=UnitKind.FUNCTION,
        language="python",
        source_snippet="def f():\n  return 1\n",
        content_hash=content_hash(uid),
        span=CodeSpan(start_line=1, end_line=2),
        embedding=emb,
    )


def test_projection_has_z_and_graph_edge(tmp_path: Path) -> None:
    rng = __import__("random").Random(1)
    units = []
    for i in range(8):
        emb = [rng.random() for _ in range(12)]
        emb[i % 12] += 3.0
        units.append(_unit(f"u{i}", emb))
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
    payload = build_projection_payload(units, [cs])
    assert payload is not None
    assert payload["point_count"] == 8
    assert all("z" in p for p in payload["points"])
    queued = [p for p in payload["points"] if p["on_queue"]]
    assert len(queued) == 2
    assert queued[0]["upside_kind"] == "dedup"

    graph = build_graph_payload([cs])
    assert graph["edge_count"] >= 1
    assert any(e["kind"] == "clone" for e in graph["edges"])

    written = write_projection_artifacts(tmp_path, units, [cs])
    assert written["projection"]
    assert written["graph"]
    data = json.loads((tmp_path / "projection.json").read_text(encoding="utf-8"))
    assert data["points"][0]["x"] is not None
    assert data["method"] in {"pca3", "tsne3"}
    g = json.loads((tmp_path / "graph.json").read_text(encoding="utf-8"))
    assert g["edge_count"] >= 1


def test_projection_none_when_too_few_embeddings() -> None:
    units = [_unit("a", [1.0, 0.0]), _unit("b", [0.0, 1.0])]
    assert build_projection_payload(units, []) is None
