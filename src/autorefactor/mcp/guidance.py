"""Shared agent workflow guidance for MCP instructions + get_agent_playbook."""

from __future__ import annotations

from typing import Any

from autorefactor.config import AutorefactorConfig
from autorefactor.config_store import get_setup_requirements, needs_setup, setup_hint

_MAX_INSTRUCTIONS_CHARS = 4000

_UI_HOME = "http://localhost:3000"
_UI_SETTINGS = f"{_UI_HOME}/settings"
_ENGINE = "http://localhost:8765"


def build_agent_guidance(config: AutorefactorConfig) -> dict[str, Any]:
    """Structured workflow / forbidden / tools map (playbook payload core)."""
    cfg = config.apply_backend_defaults()
    runtime = cfg.runtime_summary()
    agent_chat = runtime["chat_backend"] == "agent"

    # Phase CTAs: pause and guide the user between phases.
    # Inside the queue: do NOT stop on auto_proceed items — only HITL on needs_confirmation.
    workflow = [
        "PHASE RULE: Between major phases, stop and guide the user with a short status + clear "
        "next-step question (CTA). Do not dump tools silently. Exception: while walking the "
        "changeset queue, do not stop on auto_proceed=true items.",
        "Local edits: stdio MCP auto-syncs cwd (or sync_workspace). Fingerprint is the synced tree.",
        "Setup: Call get_status. If needs_setup=true, STOP and tell the user to add an API key at "
        f"{_UI_SETTINGS} (or switch to local agent+Ollama). Do not invent credentials.",
        "Analyze: If the user wants refactor/analyze OR fresh=false, call analyze(background=true) "
        "and poll until done. UI does not start analyze.",
        "After analyze finishes: Call get_report. Summarize headline metrics for the user. Give UI "
        f"links: {_UI_HOME}/runs (list), the run overview/ROI/map/queue pages for the new slug, "
        f"engine {_ENGINE}/. Then ASK: continue to the next phase, or stop here?",
    ]
    if agent_chat:
        workflow.append(
            "Enrich (agent chat only): If enrich_pending>0, ASK whether to drain enrich tasks now. "
            "Only if yes: get_next_enrich_task → work → submit_enrich_result until done=true. "
            "Then ASK before prep / queue."
        )
    else:
        workflow.append(
            "Enrich: Server-side describe already ran when chat=http. Skip enrich drain unless "
            "get_status shows enrich_pending>0 — then ASK before draining."
        )
    if runtime.get("ollama_required"):
        workflow.append(
            "If Ollama is required but down: tell the user to run "
            "`docker compose --profile ollama up -d` (Settings cannot start containers)."
        )
    describe = runtime.get("describe_mode") or "raw"
    prep_docs = (
        " (2) write generated unit descriptions into source (describe_mode=describe)?"
        if describe == "describe"
        else " (2) fill in-source doc gaps on document prep items (agent writes text)?"
    )
    workflow.extend(
        [
            "Prep (before structural refactors) — ASK once: Queue orders ensure_tests then "
            "document ahead of refactors so you can lock characterization coverage (and docs) "
            "before changing code. Ask: (1) raise test coverage first?"
            + prep_docs
            + " If no: skip those prep changesets, then continue. If yes: drain prep items "
            "first (HITL on each). document proceed writes docs"
            + (
                "; with describe_mode=describe the server applies generated descriptions."
                if describe == "describe"
                else "."
            )
            + " ensure_tests proceed scaffolds/expands tests. Do not treat write_docs / "
            "ensure_full_coverage as bolt-ons on unrelated refactors.",
            "Queue walk — ASK once: ready to walk the refactor queue? Only start after yes "
            "(or the user already asked to walk the queue).",
            "While walking: get_next_changeset ONE AT A TIME (do not batch):",
            "  • auto_proceed=true: confirm_changeset(proceed) without asking, edit, "
            "mark_changeset done, immediately fetch the next item — no mid-item CTA.",
            "  • needs_confirmation=true: STOP, show hitl_digest, ask proceed/skip/blocked "
            "before confirm_changeset.",
            "  • Respect depends_on. When queue done=true: report summary + CTA (review UI / stop).",
        ]
    )
    forbidden = [
        "Do not call prepare_workspace for a locally open project.",
        "Never put tokens or passwords in git_url — use AUTOREFACTOR_GIT_TOKEN on the server.",
        "Do not point Cursor at Docker /mcp directly if you need cwd sync — use stdio autorefactor-mcp.",
        "Do not skip phase CTAs (after analyze, before enrich, before prep, before queue) unless "
        "the user already ordered that phase in the same turn.",
        "Do not ask the user to confirm high-confidence (auto_proceed) changesets.",
        "Do not bolt write_docs onto a document changeset (proceed is writing docs).",
        "Do not bolt ensure_full_coverage onto an ensure_tests changeset (proceed is the tests).",
        "Do not drain multiple changesets before finishing the current one.",
        "Do not skip or auto-decide needs_confirmation=true changesets without asking the user.",
        "Do not treat a stale report as current when the user asked to analyze/refresh.",
        "Do not start analyze from the UI narrative; agents use MCP analyze.",
    ]
    tools = {
        "get_status": "Workspace + queue + fresh + needs_setup + enrich_pending; active_job if any.",
        "analyze": "Run sensors + queues. Prefer background=true. After done → report + UI links + CTA.",
        "get_report": "Metrics / ROI after analyze.",
        "get_next_enrich_task": "Next describe/plan task (agent chat). Ask before starting enrich phase.",
        "submit_enrich_result": "Submit JSON result for an enrich task id.",
        "get_next_changeset": (
            "ONE at a time. Prep (ensure_tests/document) is ordered first. "
            "auto_proceed → no ask; needs_confirmation → hitl_digest (must ask)."
        ),
        "confirm_changeset": (
            "decision=proceed|skip|blocked. document+describe_mode=describe → writes docs; "
            "ensure_tests → scaffolds coverage work."
        ),
        "mark_changeset": "pending|in_progress|done|skipped|blocked",
        "sync_workspace": "Force re-upload (usually unnecessary).",
        "get_agent_playbook": "Optional: live axes + setup (workflow already in MCP instructions).",
    }
    hint = setup_hint(cfg)
    return {
        "chat_backend": runtime["chat_backend"],
        "embed_backend": runtime["embed_backend"],
        "embed_device": runtime.get("embed_device"),
        "ollama_required": bool(runtime.get("ollama_required")),
        "parallel_llm": bool(runtime["parallel_llm"]),
        "describe_mode": runtime["describe_mode"],
        "embed_batch_size": runtime.get("embed_batch_size"),
        "embed_concurrency": runtime.get("embed_concurrency"),
        "effective_embed_parallel": runtime.get("effective_embed_parallel"),
        "profile": runtime["profile"],
        "workflow": workflow,
        "forbidden": forbidden,
        "tools": tools,
        "freshness": {
            "check": "get_status.fresh / freshness.reason",
            "stale_reasons": ["content_changed", "missing_fingerprint", "no_run"],
            "guidance": (
                "fresh=true → reuse report/queue OK for read-only questions; "
                "fresh=false or user wants refresh → analyze(background=true) and poll; "
                "after analyze always CTA before enrich/queue"
            ),
        },
        "needs_setup": needs_setup(cfg),
        "setup_hint": hint,
        "setup": get_setup_requirements(cfg),
        "ui": {
            "home": _UI_HOME,
            "settings": _UI_SETTINGS,
            "runs": f"{_UI_HOME}/runs",
            "engine": _ENGINE,
        },
    }


def format_mcp_instructions(guidance: dict[str, Any] | None = None) -> str:
    """Plain-text InitializeResult.instructions (concise; no enrich JSON examples)."""
    if guidance is None:
        from autorefactor.config_store import load_merged_config

        guidance = build_agent_guidance(load_merged_config())
    chat = guidance.get("chat_backend", "")
    embed = guidance.get("embed_backend", "")
    lines = [
        "autorefactor MCP — phase CTAs between analyze / enrich / prep / queue; "
        "inside the queue only HITL on needs_confirmation (never on auto_proceed).",
        f"Runtime hint at server start: chat={chat} embed={embed}.",
    ]
    if guidance.get("needs_setup"):
        lines.append(f"SETUP REQUIRED: {guidance.get('setup_hint') or 'Missing API key.'}")
    lines.append("")
    lines.append("Workflow:")
    for step in guidance.get("workflow") or []:
        lines.append(f"- {step}")
    lines.append("")
    lines.append("Forbidden:")
    for step in guidance.get("forbidden") or []:
        lines.append(f"- {step}")
    lines.append("")
    lines.append(
        "Call get_agent_playbook when you need current chat/embed axes, setup requirements, "
        "or enrich submit examples."
    )
    text = "\n".join(lines)
    if len(text) > _MAX_INSTRUCTIONS_CHARS:
        text = text[: _MAX_INSTRUCTIONS_CHARS - 1] + "…"
    return text
