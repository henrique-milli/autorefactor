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
        "Local projects: call sync_workspace(path='/abs/path/to/project') then analyze(path=<same>, background=true). "
        "No git required — upload is tar.gz over HTTP. CLI: autorefactor sync -w PATH. "
        "On sync failure: use CLI `autorefactor sync -w PATH` then retry analyze — do not thrash alternate paths.",
        "Setup: Call get_status. If needs_setup=true, STOP and tell the user to add an API key at "
        f"{_UI_SETTINGS} (or switch to local agent+Ollama). Do not invent credentials.",
        "Analyze: If the user wants refactor/analyze OR fresh=false, call analyze(background=true) "
        "and poll until done. UI does not start analyze.",
        "After analyze finishes: Call get_report. Summarize headline metrics for the user. Give UI "
        f"links: {_UI_HOME}/runs (list), the run overview/ROI/map/queue pages for the new slug, "
        f"engine {_ENGINE}/. Always include the queue URL ({_UI_HOME}/runs/<slug>/queue): user can "
        "Approve/Skip HITL there, then tell you to execute the approved items. "
        "Mention prep coverage yes/no before any structural walk. "
        "ASK: review/Approve in the UI now, continue to prep, or stop here?",
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
    if describe == "describe":
        prep_docs = (
            "Prep docs (optional, same prep stop): write generated unit descriptions into source "
            "(describe_mode=describe)? If no: skip document prep. If yes: drain with auto_proceed "
            "unless needs_confirmation (document only)."
        )
    else:
        prep_docs = (
            "Prep docs: describe_mode=raw — do not queue document prep (no generated text to apply). "
            "Document ad hoc in source only if the user asks."
        )
    workflow.extend(
        [
            "Prep coverage (ASK ONCE before queue walk): Warn that structural refactors are "
            "queued without full test coverage. Ask exactly: 'You are about to refactor without "
            "full test coverage. Push coverage to the limit on all flagged production code before "
            "any structural refactors? (yes/no)'. If yes: drain every ensure_tests item with "
            "auto_proceed — confirm_changeset(proceed) without asking per item. If no: "
            "confirm_changeset(skip) on each ensure_tests without asking, then structural queue. "
            "NEVER ask proceed/skip/blocked on individual ensure_tests — prep yes/no covers all.",
            prep_docs,
            "Queue walk — start only after prep coverage yes/no is answered (or user already "
            "ordered queue walk). If the UI has Approve decisions pending, get_next returns only "
            "those until drained; then resume normal HITL/autorun.",
            "While walking: get_next_changeset ONE AT A TIME (do not batch):",
            "  • auto_proceed=true: confirm_changeset(proceed) without asking, edit, "
            "mark_changeset done, immediately fetch the next item — no mid-item CTA "
            "(includes UI-approved user_decision=proceed).",
            "  • needs_confirmation=true: STOP, present hitl_review as a compact card — title, "
            "model rationale (hitl_review.rationale / Why flagged), optional benefit, "
            "code snippet(s); if compare_targets=true show both clones side-by-side, then ask "
            "proceed/skip/blocked (never for ensure_tests). hitl_reason=plan_review means the "
            "planner asked for human judgment (not a weak auto-keep). Or point user to Approve/Skip in "
            "the queue UI, then execute when they return.",
            "  • loop=waiting / blocked_deps: not a user question — drain or skip blocker first.",
            "  • Respect depends_on. When queue done=true: remind to run the project test suite "
            "once (mvn test / repo equivalent) before claiming done; then CTA (review UI / stop).",
        ]
    )
    forbidden = [
        "Do not call prepare_workspace for a locally open project.",
        "Never put tokens or passwords in git_url — use AUTOREFACTOR_GIT_TOKEN on the server.",
        "Do not point Cursor at Docker /mcp directly if you need cwd sync — use stdio autorefactor-mcp.",
        "Do not skip phase CTAs (after analyze, before enrich, before prep, before queue) unless "
        "the user already ordered that phase in the same turn.",
        "Do not ask the user to confirm high-confidence (auto_proceed) changesets.",
        "Do not ask proceed/skip/blocked on individual ensure_tests items — one prep coverage yes/no only.",
        "Do not bolt write_docs onto a document changeset (proceed is writing docs).",
        "Do not bolt ensure_full_coverage onto an ensure_tests changeset (proceed is the tests).",
        "Do not drain multiple changesets before finishing the current one.",
        "Do not skip or auto-decide needs_confirmation=true changesets without asking the user.",
        "Do not present HITL as text-only — always show relevant code snippets from hitl_review.targets.",
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
            "ONE at a time. ensure_tests always auto_proceed (prep coverage yes/no is the only gate). "
            "UI user_decision=proceed → auto_proceed (do not re-ask). "
            "Other needs_confirmation → hitl_review (structured card with snippets + choices)."
        ),
        "confirm_changeset": (
            "decision=proceed|skip|blocked. document+describe_mode=describe → writes docs; "
            "ensure_tests → scaffolds coverage work."
        ),
        "mark_changeset": "pending|in_progress|done|skipped|blocked",
        "sync_workspace": "Upload local project: path='/abs/path/to/project' (tar.gz HTTP). CLI: autorefactor sync -w PATH.",
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
        "ensure_tests never per-item HITL (one prep coverage yes/no); other needs_confirmation only. "
        "After analyze: give queue UI link for Approve/Skip, then execute when the user asks.",
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
