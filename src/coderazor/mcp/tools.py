from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from coderazor.artifacts import layout_for
from coderazor.changesets import ChangesetStatus, ChangesetStore
from coderazor.config import CoderazorConfig
from coderazor.config_store import (
    get_setup_requirements,
    load_merged_config,
    needs_setup,
    save_config,
)
from coderazor.describe import describe_units
from coderazor.index.store import IndexStore
from coderazor.stages.analyze import build_provider, run_analyze


def tool_get_setup_requirements(workspace: str | None = None) -> dict[str, Any]:
    config = load_merged_config(workspace=Path(workspace) if workspace else None)
    reqs = get_setup_requirements(config)
    return {"needs_setup": bool(reqs), "requirements": reqs}


def tool_save_config(partial: dict[str, Any], user_level: bool = False) -> dict[str, Any]:
    path = save_config(partial, user_level=user_level)
    return {"ok": True, "path": str(path)}


def tool_analyze(
    workspace: str,
    out: str = "out",
    skip_llm: bool = False,
    describe_mode: str | None = None,
) -> dict[str, Any]:
    config = load_merged_config(
        workspace=Path(workspace),
        out_dir=Path(out),
        cli_overrides={
            "skip_llm": skip_llm,
            **({"describe_mode": describe_mode} if describe_mode else {}),
        },
    )
    config.skip_llm = skip_llm or config.skip_llm
    if describe_mode:
        config.describe_mode = describe_mode
    if needs_setup(config) and not skip_llm:
        return {"ok": False, "error": "needs_setup", **tool_get_setup_requirements(workspace)}
    artifacts = run_analyze(config)
    layout = layout_for(config.resolve_out_dir())
    return {
        "ok": True,
        "confirmed": artifacts.manifest.confirmed_count if artifacts.manifest else 0,
        "flags": artifacts.manifest.flag_count if artifacts.manifest else 0,
        "analyze_dir": str(layout.analyze_dir),
        "report_md": str(layout.out_dir / "report" / "report.md"),
        "queue": str(layout.out_dir / "changesets" / "queue.json"),
    }


def tool_get_report(out: str = "out") -> dict[str, Any]:
    out_dir = Path(out)
    report_md = out_dir / "report" / "report.md"
    metrics = out_dir / "report" / "metrics.json"
    payload: dict[str, Any] = {
        "report_md": str(report_md) if report_md.exists() else None,
        "metrics_json": str(metrics) if metrics.exists() else None,
        "visuals_dir": str(out_dir / "report" / "visuals"),
    }
    if metrics.exists():
        payload["metrics"] = json.loads(metrics.read_text(encoding="utf-8"))
    return payload


def _store(out: str) -> ChangesetStore:
    return ChangesetStore(Path(out) / "changesets" / "queue.json")


def tool_list_changesets(out: str = "out") -> dict[str, Any]:
    items = _store(out).list()
    return {
        "changesets": [
            {
                "id": c.id,
                "title": c.title,
                "action": c.action,
                "confidence": c.confidence,
                "status": c.status.value,
                "parallel_group": c.parallel_group,
            }
            for c in items
        ]
    }


def tool_get_next_changeset(out: str = "out", min_confidence: float | None = None) -> dict[str, Any]:
    config = load_merged_config()
    threshold = config.confidence_threshold if min_confidence is None else min_confidence
    result = _store(out).next_pending(min_confidence=threshold)
    if result.done:
        return {"done": True}
    assert result.changeset is not None
    return {
        "done": False,
        "needs_confirmation": result.needs_confirmation,
        "questions": result.questions,
        "changeset": result.changeset.model_dump(mode="json"),
    }


def tool_get_changeset(out: str, changeset_id: str) -> dict[str, Any]:
    item = _store(out).get(changeset_id)
    if item is None:
        return {"ok": False, "error": "not_found"}
    return {"ok": True, "changeset": item.model_dump(mode="json")}


def tool_mark_changeset(
    out: str,
    changeset_id: str,
    status: str,
    notes: str = "",
) -> dict[str, Any]:
    updated = _store(out).mark(changeset_id, ChangesetStatus(status), notes=notes)
    return {"ok": True, "changeset": updated.model_dump(mode="json")}


def tool_describe_unit(
    workspace: str,
    out: str,
    unit_id: str,
    skip_llm: bool = False,
) -> dict[str, Any]:
    from coderazor.artifacts import load_units

    config = load_merged_config(workspace=Path(workspace), out_dir=Path(out))
    config.skip_llm = skip_llm or config.skip_llm
    units = load_units(layout_for(Path(out)))
    unit = next((u for u in units if u.id == unit_id), None)
    if unit is None:
        return {"ok": False, "error": "unit_not_found"}
    provider = build_provider(config)
    store = IndexStore(Path(out) / "cache" / "index.db")
    described = describe_units([unit], provider, store, concurrency=1)
    store.close()
    u = described[0]
    return {
        "ok": True,
        "unit_id": u.id,
        "description": u.description,
        "tags": u.tags,
    }
