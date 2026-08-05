from __future__ import annotations

from pathlib import Path

from coderazor.changesets.models import Changeset
from coderazor.models import CodeUnit, ConfirmedFinding, Flag, FlagSource


def estimate_metrics(
    *,
    units: list[CodeUnit],
    flags: list[Flag],
    findings: list[ConfirmedFinding],
    changesets: list[Changeset],
) -> dict:
    files = {(u.repo, u.path) for u in units}
    loc = 0
    for unit in units:
        if unit.kind.value == "file":
            loc += max(1, unit.span.end_line or unit.source_snippet.count("\n") + 1)
    if loc == 0:
        loc = sum(max(1, u.source_snippet.count("\n") + 1) for u in units if u.kind.value == "function")

    confirmed = [f for f in findings if f.valid]
    est_loc = sum(int(cs.metrics_delta.get("estimated_loc_removed") or 0) for cs in changesets)
    # Token proxy: ~4 chars per token on removable snippets
    removable_chars = 0
    by_id = {u.id: u for u in units}
    for cs in changesets:
        for t in cs.targets:
            if t.unit_id and t.unit_id in by_id:
                removable_chars += len(by_id[t.unit_id].source_snippet)
    est_tokens = removable_chars // 4
    baseline_tokens = max(1, sum(len(u.source_snippet) for u in units) // 4)
    token_pct = 100.0 * est_tokens / baseline_tokens

    debt = 0.0
    for flag in flags:
        if flag.source in {FlagSource.SMELL, FlagSource.ANALYZER}:
            debt += flag.score * (2.0 if flag.severity.value == "high" else 1.0)

    dup_paths = {
        t.path
        for cs in changesets
        if cs.action in {"merge", "extract_shared", "delete_duplicate"}
        for t in cs.targets
    }

    return {
        "exact": {
            "unit_count": len(units),
            "file_count": len(files),
            "loc": loc,
            "flag_count": len(flags),
            "confirmed_count": len(confirmed),
            "changeset_count": len(changesets),
        },
        "estimates": {
            "expected_loc_reduction": {
                "value": est_loc,
                "label": "estimate",
                "method": "sum of changeset metrics_delta.estimated_loc_removed",
            },
            "expected_token_reduction_percent": {
                "value": round(token_pct, 2),
                "label": "estimate",
                "method": "removable snippet chars / 4 vs baseline",
            },
        },
        "scores": {
            "tech_debt": {
                "value": round(debt, 2),
                "label": "score",
                "method": "weighted smell/analyzer flag scores",
            },
            "risk_surface_duplicate_paths": {
                "value": len(dup_paths),
                "label": "count",
                "method": "distinct paths targeted by merge/extract/delete changesets",
            },
        },
    }


def write_metrics_and_report(
    report_dir: Path,
    *,
    metrics: dict,
    changesets: list[Changeset],
    visual_paths: list[str],
) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    import json

    (report_dir / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    exact = metrics["exact"]
    lines = [
        "# coderazor analysis report",
        "",
        "## Exact baseline",
        "",
        f"- Units: **{exact['unit_count']}**",
        f"- Files: **{exact['file_count']}**",
        f"- LOC (approx from spans): **{exact['loc']}**",
        f"- Flags: **{exact['flag_count']}** (confirmed {exact['confirmed_count']})",
        f"- Changesets: **{exact['changeset_count']}**",
        "",
        "## Estimates (labeled)",
        "",
    ]
    for key, row in metrics["estimates"].items():
        lines.append(f"- {key}: **{row['value']}** _{row['label']}_ — {row['method']}")
    lines.extend(["", "## Scores", ""])
    for key, row in metrics["scores"].items():
        lines.append(f"- {key}: **{row['value']}** _{row['label']}_ — {row['method']}")
    lines.extend(["", "## Changesets", ""])
    for cs in changesets[:50]:
        lines.append(f"- `{cs.id}` **{cs.title}** ({cs.action}, confidence={cs.confidence:.2f})")
    lines.extend(["", "## Visuals", ""])
    if visual_paths:
        for path in visual_paths:
            lines.append(f"- `{path}`")
    else:
        lines.append("- (none)")
    lines.append("")
    (report_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    (report_dir / "report.json").write_text(
        json.dumps(
            {"metrics": metrics, "changeset_ids": [c.id for c in changesets], "visuals": visual_paths},
            indent=2,
        ),
        encoding="utf-8",
    )
