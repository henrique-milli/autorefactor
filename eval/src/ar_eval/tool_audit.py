"""Audit claimed MCP tool use in product-user feedback."""

from __future__ import annotations

from typing import Any

CORE_TOOLS = frozenset({"get_status", "get_report"})
QUEUE_SAMPLE_TOOL = "sample_queue"
QUEUE_SAMPLE_FALLBACK = frozenset({"list_changesets", "get_changeset"})

APPLY_TOOLS = frozenset(
    {
        "confirm_changeset",
        "mark_changeset",
    }
)

KNOWN_TOOLS = frozenset(
    {
        "get_status",
        "get_report",
        "sample_queue",
        "get_next_changeset",
        "confirm_changeset",
        "mark_changeset",
        "list_changesets",
        "get_changeset",
        "list_enrich_tasks",
        "get_next_enrich_task",
        "submit_enrich_result",
        "get_agent_playbook",
        "workspace_status",
        "analyze",
        "get_progress",
        "describe_unit",
    }
)


def normalize_tool_name(name: str) -> str:
    n = str(name or "").strip().lower()
    # Agents sometimes report "user-autorefactor-get_report" style names.
    for prefix in ("user-autorefactor-", "autorefactor-", "mcp_"):
        if n.startswith(prefix):
            n = n[len(prefix) :]
    if n.startswith("user-autorefactor"):
        n = n.split("-", 2)[-1] if "-" in n else n
    return n.replace(" ", "_")


def collect_claimed_tools(feedback: dict[str, Any]) -> list[str]:
    lifecycle = feedback.get("lifecycle") if isinstance(feedback.get("lifecycle"), dict) else {}
    raw = list(lifecycle.get("mcp_tools_used") or [])
    # Also harvest from evidence notes / fields
    for f in feedback.get("findings") or []:
        if not isinstance(f, dict):
            continue
        for ev in f.get("evidence") or []:
            if not isinstance(ev, dict):
                continue
            note = str(ev.get("note") or "")
            for tok in note.replace(",", " ").split():
                nt = normalize_tool_name(tok.strip("`.`()[]"))
                if nt in KNOWN_TOOLS:
                    raw.append(nt)
            for key in ("tool", "mcp_tool"):
                if ev.get(key):
                    raw.append(str(ev.get(key)))
    out: list[str] = []
    seen: set[str] = set()
    for t in raw:
        nt = normalize_tool_name(t)
        if nt and nt not in seen:
            seen.add(nt)
            out.append(nt)
    return out


def _missing_required(tools: set[str]) -> list[str]:
    missing = sorted(CORE_TOOLS - tools)
    sampled = QUEUE_SAMPLE_TOOL in tools or QUEUE_SAMPLE_FALLBACK <= tools
    if not sampled:
        missing.append("sample_queue_or_list+get_changeset")
    return missing


def audit_mcp_tools(feedback: dict[str, Any]) -> dict[str, Any]:
    """
    Hard-ish check that feedback claims a real MCP sample walk.

    Returns ``{ok, reasons, tools_used, checks}``.
    """
    reasons: list[str] = []
    tools = collect_claimed_tools(feedback)
    toolset = set(tools)
    checks: dict[str, Any] = {"tools_used": tools}
    missing_required = _missing_required(toolset)
    checks["missing_required"] = missing_required
    if missing_required:
        reasons.append("mcp_tools_missing_required")

    mutate = sorted(APPLY_TOOLS & toolset)
    checks["mutate_tools_seen"] = mutate
    if mutate:
        reasons.append("must_not_mutate_queue")

    unknown = sorted(t for t in tools if t not in KNOWN_TOOLS)
    checks["unknown_tools"] = unknown
    # Unknown tools are soft — do not fail the gate.

    return {
        "ok": len(reasons) == 0,
        "reasons": reasons,
        "tools_used": tools,
        "checks": checks,
    }
