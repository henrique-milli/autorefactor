from __future__ import annotations

import json
import os
from typing import Any

from autorefactor.mcp import tools as T
from autorefactor.workspace_sync import (
    clone_git_workspace,
    export_active_tar_gz,
    import_tar_gz,
    workspace_status,
)


def _tool_list() -> list[dict[str, Any]]:
    return [
        {
            "name": "workspace_status",
            "description": (
                "Whether the active workspace is synced (file_count, source, root). "
                "With local stdio MCP + Docker, the open project is uploaded automatically."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "sync_workspace",
            "description": (
                "Upload a local project to the remote engine (tar.gz over HTTP). "
                "Pass path='/absolute/path/to/project'. No git required. "
                "CLI equivalent: autorefactor sync -w PATH. Required before analyze "
                "when cwd is not the project root."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the local project directory to upload.",
                    },
                },
            },
        },
        {
            "name": "prepare_workspace",
            "description": (
                "Advanced only: clone remote git repo(s) into workspace=active when there "
                "is no local tree. Do not use for local projects. "
                "Never put tokens in git_url — set AUTOREFACTOR_GIT_TOKEN on the server."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "git_url": {
                        "type": "string",
                        "description": "Single https/http/ssh/git@ URL (no local paths).",
                    },
                    "git_urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Multiple repo URLs cloned as siblings under active.",
                    },
                    "ref": {
                        "type": "string",
                        "description": "Optional branch or tag for shallow clone(s).",
                    },
                },
            },
        },
        {
            "name": "get_setup_requirements",
            "description": "Return missing autorefactor config fields and questions for the user.",
            "inputSchema": {
                "type": "object",
                "properties": {"workspace": {"type": "string"}},
            },
        },
        {
            "name": "get_agent_playbook",
            "description": (
                "Optional: live chat/embed axes. Core workflow is already in MCP "
                "InitializeResult.instructions — call this for axes, setup, enrich examples."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "workspace": {"type": "string"},
                    "out": {"type": "string"},
                },
            },
        },
        {
            "name": "list_enrich_tasks",
            "description": "List describe/plan enrich tasks for the host agent queue.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "out": {"type": "string"},
                    "workspace": {"type": "string"},
                },
            },
        },
        {
            "name": "get_next_enrich_task",
            "description": (
                "Next pending enrich task (describe or plan). Submit result with "
                "submit_enrich_result. Agent chat only — empty when chat is HTTP."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "out": {"type": "string"},
                    "workspace": {"type": "string"},
                },
            },
        },
        {
            "name": "submit_enrich_result",
            "description": (
                "Submit JSON result for an enrich task. describe: {doc, tags}; "
                "plan: {decisions: [...]}."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "result": {"type": "object"},
                    "out": {"type": "string"},
                    "workspace": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["task_id", "result"],
            },
        },
        {
            "name": "save_config",
            "description": "Persist autorefactor settings to .autorefactor.toml (or user config).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "partial": {"type": "object"},
                    "user_level": {"type": "boolean"},
                },
                "required": ["partial"],
            },
        },
        {
            "name": "analyze",
            "description": (
                "Analyze a local project on the remote engine. Pass path='/absolute/path/to/project' "
                "(syncs via tar upload unless product bind-mount). describe_mode=raw|describe. "
                "For long runs set background=true and poll get_progress."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute local project path to sync and analyze.",
                    },
                    "workspace": {
                        "type": "string",
                        "description": "Remote selector only: active|mounted. Do not pass host paths.",
                    },
                    "out": {"type": "string"},
                    "skip_llm": {"type": "boolean"},
                    "describe_mode": {
                        "type": "string",
                        "description": "raw | describe (aliases embed_only, index). No write.",
                    },
                    "focus": {"type": "string"},
                    "background": {
                        "type": "boolean",
                        "description": "Default true: return job_id immediately; poll get_progress.",
                    },
                },
            },
        },
        {
            "name": "list_jobs",
            "description": "List recent analyze jobs (id, status, phase, percent) newest first.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer"},
                    "status": {
                        "type": "string",
                        "description": "Optional filter: running|done|failed",
                    },
                },
            },
        },
        {
            "name": "get_progress",
            "description": (
                "Fetch analyze job progress (phase, percent, detail, events). "
                "Pass job_id, or omit to resolve active/latest for workspace/out."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "workspace": {"type": "string"},
                    "out": {"type": "string"},
                },
            },
        },
        {
            "name": "get_analyze_job",
            "description": "Alias of get_progress(job_id=…); poll a background analyze job.",
            "inputSchema": {
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
        },
        {
            "name": "get_status",
            "description": (
                "Setup + queue progress for the workspace (changeset counts, report ready, "
                "active analyze job if any). Includes fresh / freshness.reason and "
                "resolved_workspace — check freshness before answering from a report."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "workspace": {"type": "string"},
                    "out": {"type": "string"},
                },
            },
        },
        {
            "name": "get_report",
            "description": (
                "Return report paths and metric highlights. Stale runs still return "
                "ok=true with a STALE banner and metrics; re-analyze before applying."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "out": {"type": "string"},
                    "workspace": {"type": "string"},
                    "allow_stale": {
                        "type": "boolean",
                        "description": "If true, return metrics even when fresh=false (debug).",
                        "default": False,
                    },
                },
            },
        },
        {
            "name": "sample_queue",
            "description": (
                "Read-only queue peek: HITL burden, one auto_proceed sample, "
                "and 1–3 lowest-confidence HITL items with hitl_review (snippets + choices). "
                "Does not confirm, mark, or change queue status."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "out": {"type": "string"},
                    "workspace": {"type": "string"},
                    "hitl_limit": {
                        "type": "integer",
                        "description": "Max HITL samples (default 3, max 8).",
                        "default": 3,
                    },
                },
            },
        },
        {
            "name": "list_changesets",
            "description": "Compact list of changesets (id, title, confidence, status).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "out": {"type": "string"},
                    "workspace": {"type": "string"},
                },
            },
        },
        {
            "name": "get_next_changeset",
            "description": (
                "Next pending changeset (ensure_tests/document prep ordered first). "
                "auto_proceed when confidence ≥ threshold or UI user_decision=proceed; "
                "needs_confirmation + hitl_review (code snippets, rationale, choices) when "
                "judgment required."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "out": {"type": "string"},
                    "workspace": {"type": "string"},
                    "min_confidence": {"type": "number"},
                },
            },
        },
        {
            "name": "get_changeset",
            "description": "Fetch a changeset by id.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "out": {"type": "string"},
                    "workspace": {"type": "string"},
                    "changeset_id": {"type": "string"},
                },
                "required": ["changeset_id"],
            },
        },
        {
            "name": "mark_changeset",
            "description": "Update changeset status: pending|in_progress|done|skipped|blocked.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "out": {"type": "string"},
                    "workspace": {"type": "string"},
                    "changeset_id": {"type": "string"},
                    "status": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["changeset_id", "status"],
            },
        },
        {
            "name": "confirm_changeset",
            "description": (
                "Confirm a changeset: decision=proceed|skip|blocked. "
                "document+describe_mode=describe writes docs on proceed; "
                "ensure_tests scaffolds coverage work on proceed."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "changeset_id": {"type": "string"},
                    "decision": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "out": {"type": "string"},
                    "workspace": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["changeset_id"],
            },
        },
        {
            "name": "describe_unit",
            "description": (
                "On-demand LLM describe for one indexed unit id. Returns analysis-index "
                "metadata (description/tags) only — does NOT write source Javadocs/** */ "
                "or add tests; use the host agent for that."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "workspace": {"type": "string"},
                    "out": {"type": "string"},
                    "unit_id": {"type": "string"},
                    "skip_llm": {"type": "boolean"},
                },
                "required": ["unit_id"],
            },
        },
    ]


def dispatch(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "workspace_status":
        return T.tool_workspace_status()
    if name == "sync_workspace":
        return T.tool_sync_workspace(arguments.get("path"))
    if name == "prepare_workspace":
        return T.tool_prepare_workspace(
            arguments.get("git_url"),
            arguments.get("ref"),
            arguments.get("git_urls"),
        )
    if name == "get_setup_requirements":
        return T.tool_get_setup_requirements(arguments.get("workspace"))
    if name == "get_agent_playbook":
        return T.tool_get_agent_playbook(arguments.get("workspace"), arguments.get("out"))
    if name == "list_enrich_tasks":
        return T.tool_list_enrich_tasks(arguments.get("out"), arguments.get("workspace"))
    if name == "get_next_enrich_task":
        return T.tool_get_next_enrich_task(arguments.get("out"), arguments.get("workspace"))
    if name == "submit_enrich_result":
        return T.tool_submit_enrich_result(
            arguments["task_id"],
            arguments.get("result") or {},
            arguments.get("out"),
            arguments.get("workspace"),
            arguments.get("notes") or "",
        )
    if name == "save_config":
        return T.tool_save_config(arguments.get("partial") or {}, bool(arguments.get("user_level")))
    if name == "analyze":
        return T.tool_analyze(
            arguments.get("workspace"),
            arguments.get("out"),
            bool(arguments.get("skip_llm")),
            arguments.get("describe_mode"),
            arguments.get("focus"),
            bool(arguments.get("background", True)),
            arguments.get("path"),
        )
    if name == "get_analyze_job":
        return T.tool_get_analyze_job(arguments["job_id"])
    if name == "list_jobs":
        limit = arguments.get("limit")
        return T.tool_list_jobs(
            int(limit) if limit is not None else 20,
            arguments.get("status"),
        )
    if name == "get_progress":
        return T.tool_get_progress(
            arguments.get("job_id"),
            arguments.get("workspace"),
            arguments.get("out"),
        )
    if name == "get_status":
        return T.tool_get_status(arguments.get("workspace"), arguments.get("out"))
    if name == "get_report":
        return T.tool_get_report(
            arguments.get("out"),
            arguments.get("workspace"),
            bool(arguments.get("allow_stale") or False),
        )
    if name == "sample_queue":
        return T.tool_sample_queue(
            arguments.get("out"),
            arguments.get("workspace"),
            int(arguments.get("hitl_limit") or 3),
        )
    if name == "list_changesets":
        return T.tool_list_changesets(arguments.get("out"), arguments.get("workspace"))
    if name == "get_next_changeset":
        return T.tool_get_next_changeset(
            arguments.get("out"),
            arguments.get("workspace"),
            arguments.get("min_confidence"),
        )
    if name == "get_changeset":
        return T.tool_get_changeset(
            arguments["changeset_id"],
            arguments.get("out"),
            arguments.get("workspace"),
        )
    if name == "mark_changeset":
        return T.tool_mark_changeset(
            arguments["changeset_id"],
            arguments["status"],
            arguments.get("out"),
            arguments.get("workspace"),
            arguments.get("notes") or "",
        )
    if name == "confirm_changeset":
        return T.tool_confirm_changeset(
            arguments["changeset_id"],
            arguments.get("decision") or "proceed",
            arguments.get("options"),
            arguments.get("out"),
            arguments.get("workspace"),
            arguments.get("notes") or "",
        )
    if name == "describe_unit":
        return T.tool_describe_unit(
            arguments.get("workspace"),
            arguments["unit_id"],
            arguments.get("out"),
            bool(arguments.get("skip_llm")),
        )
    return {"ok": False, "error": f"unknown tool {name}"}


def _register_tools(mcp: Any) -> None:
    @mcp.tool()
    def workspace_status() -> dict:
        """Whether the active workspace is synced (file_count, source, root)."""
        return T.tool_workspace_status()

    @mcp.tool()
    def sync_workspace(path: str | None = None) -> dict:
        """Upload local project tree to remote engine. Pass path='/abs/path/to/project'."""
        return T.tool_sync_workspace(path)

    @mcp.tool()
    def prepare_workspace(
        git_url: str | None = None,
        ref: str | None = None,
        git_urls: list[str] | None = None,
    ) -> dict:
        """Advanced only: clone remote git into active. Not for local projects or describe write."""
        return T.tool_prepare_workspace(git_url, ref, git_urls)

    @mcp.tool()
    def get_setup_requirements(workspace: str | None = None) -> dict:
        """Return missing autorefactor config fields and questions for the user."""
        return T.tool_get_setup_requirements(workspace)

    @mcp.tool()
    def get_agent_playbook(workspace: str | None = None, out: str | None = None) -> dict:
        """Live chat/embed axes; core workflow is in MCP instructions."""
        return T.tool_get_agent_playbook(workspace, out)

    @mcp.tool()
    def list_enrich_tasks(out: str | None = None, workspace: str | None = None) -> dict:
        """List describe/plan enrich tasks."""
        return T.tool_list_enrich_tasks(out, workspace)

    @mcp.tool()
    def get_next_enrich_task(out: str | None = None, workspace: str | None = None) -> dict:
        """Next pending enrich task for the host agent."""
        return T.tool_get_next_enrich_task(out, workspace)

    @mcp.tool()
    def submit_enrich_result(
        task_id: str,
        result: dict,
        out: str | None = None,
        workspace: str | None = None,
        notes: str = "",
    ) -> dict:
        """Submit validated JSON for an enrich task."""
        return T.tool_submit_enrich_result(task_id, result, out, workspace, notes)

    @mcp.tool()
    def save_config(partial: dict, user_level: bool = False) -> dict:
        """Persist autorefactor settings to .autorefactor.toml (or user config)."""
        return T.tool_save_config(partial, user_level)

    @mcp.tool()
    def analyze(
        path: str | None = None,
        workspace: str | None = None,
        out: str | None = None,
        skip_llm: bool = False,
        describe_mode: str | None = None,
        focus: str | None = None,
        background: bool = True,
    ) -> dict:
        """Sync path= then analyze on remote. background=true returns job_id immediately."""
        return T.tool_analyze(
            workspace, out, skip_llm, describe_mode, focus, background, path
        )

    @mcp.tool()
    def list_jobs(limit: int = 20, status: str | None = None) -> dict:
        """List recent analyze jobs (newest first)."""
        return T.tool_list_jobs(limit, status)

    @mcp.tool()
    def get_progress(
        job_id: str | None = None,
        workspace: str | None = None,
        out: str | None = None,
    ) -> dict:
        """Fetch analyze job progress; omit job_id to resolve active/latest for workspace."""
        return T.tool_get_progress(job_id, workspace, out)

    @mcp.tool()
    def get_analyze_job(job_id: str) -> dict:
        """Alias of get_progress(job_id=…); poll a background analyze job."""
        return T.tool_get_analyze_job(job_id)

    @mcp.tool()
    def get_status(workspace: str | None = None, out: str | None = None) -> dict:
        """Setup + queue progress. Check fresh / freshness.reason before using a report."""
        return T.tool_get_status(workspace, out)

    @mcp.tool()
    def get_report(
        out: str | None = None,
        workspace: str | None = None,
        allow_stale: bool = False,
    ) -> dict:
        """Report metrics; stale runs include a STALE banner (still ok=true)."""
        return T.tool_get_report(out, workspace, allow_stale=allow_stale)

    @mcp.tool()
    def sample_queue(
        out: str | None = None,
        workspace: str | None = None,
        hitl_limit: int = 3,
    ) -> dict:
        """Read-only HITL burden + sample briefs. Does not mutate the queue."""
        return T.tool_sample_queue(out, workspace, hitl_limit)

    @mcp.tool()
    def list_changesets(out: str | None = None, workspace: str | None = None) -> dict:
        return T.tool_list_changesets(out, workspace)

    @mcp.tool()
    def get_next_changeset(
        out: str | None = None,
        workspace: str | None = None,
        min_confidence: float | None = None,
    ) -> dict:
        return T.tool_get_next_changeset(out, workspace, min_confidence)

    @mcp.tool()
    def get_changeset(
        changeset_id: str,
        out: str | None = None,
        workspace: str | None = None,
    ) -> dict:
        return T.tool_get_changeset(changeset_id, out, workspace)

    @mcp.tool()
    def mark_changeset(
        changeset_id: str,
        status: str,
        out: str | None = None,
        workspace: str | None = None,
        notes: str = "",
    ) -> dict:
        return T.tool_mark_changeset(changeset_id, status, out, workspace, notes)

    @mcp.tool()
    def confirm_changeset(
        changeset_id: str,
        decision: str = "proceed",
        options: list | None = None,
        out: str | None = None,
        workspace: str | None = None,
        notes: str = "",
    ) -> dict:
        """Proceed/skip/blocked. Prep actions apply their work on proceed."""
        return T.tool_confirm_changeset(
            changeset_id, decision, options, out, workspace, notes
        )

    @mcp.tool()
    def describe_unit(
        unit_id: str,
        workspace: str | None = None,
        out: str | None = None,
        skip_llm: bool = False,
    ) -> dict:
        """LLM describe for one unit — analysis index metadata only.

        Does not write source Javadocs or add tests; the host agent must edit code.
        """
        return T.tool_describe_unit(workspace, unit_id, out, skip_llm)


def _register_http_routes(mcp: Any) -> None:
    from starlette.concurrency import run_in_threadpool
    from starlette.requests import Request
    from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response

    from autorefactor.http import runs_api
    from autorefactor.runtime_status import runtime_health_payload, status_html

    def _json(
        data: Any,
        status_code: int = 200,
        request: Request | None = None,
        *,
        allow_mutations: bool = False,
    ) -> JSONResponse:
        headers = runs_api.cors_headers(
            request.headers.get("origin") if request else None,
            allow_mutations=allow_mutations,
        )
        return JSONResponse(data, status_code=status_code, headers=headers)

    def _options(request: Request, *, allow_mutations: bool = False) -> Response:
        return Response(
            status_code=204,
            headers=runs_api.cors_headers(
                request.headers.get("origin"),
                allow_mutations=allow_mutations,
            ),
        )

    @mcp.custom_route("/", methods=["GET"])
    async def root(_request: Request) -> Response:
        return HTMLResponse(status_html())

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> Response:
        return JSONResponse(runtime_health_payload())

    @mcp.custom_route("/v1/workspace", methods=["GET"])
    async def workspace_get(_request: Request) -> Response:
        return JSONResponse(await run_in_threadpool(workspace_status))

    @mcp.custom_route("/v1/workspace/sync", methods=["POST"])
    async def workspace_sync(request: Request) -> Response:
        """Accept a gzipped tar of the working tree (tracked + untracked non-ignored)."""
        label = request.query_params.get("label")
        source = request.query_params.get("source")
        content_type = (request.headers.get("content-type") or "").lower()
        try:
            if "multipart/form-data" in content_type:
                form = await request.form()
                upload = form.get("file") or form.get("archive")
                if upload is None:
                    return JSONResponse({"ok": False, "error": "missing file field"}, status_code=400)
                data = await upload.read()  # type: ignore[union-attr]
                label = label or getattr(upload, "filename", None)
            else:
                data = await request.body()
            if not data:
                return JSONResponse({"ok": False, "error": "empty body"}, status_code=400)
            result = await run_in_threadpool(import_tar_gz, data, label=label, source=source)
            return JSONResponse(result)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)[:500]}, status_code=400)

    @mcp.custom_route("/v1/workspace/export", methods=["GET"])
    async def workspace_export(request: Request) -> Response:
        """Return the active workspace as a gzipped tar (for describe-write pull-back)."""
        paths_raw = request.query_params.get("paths")
        paths = [p.strip() for p in paths_raw.split(",") if p.strip()] if paths_raw else None
        try:
            data = await run_in_threadpool(export_active_tar_gz, paths=paths)
        except RuntimeError as exc:
            return JSONResponse({"ok": False, "error": str(exc)[:500]}, status_code=404)
        assert isinstance(data, (bytes, bytearray))
        return Response(
            content=bytes(data),
            media_type="application/gzip",
            headers={"Content-Disposition": "attachment; filename=workspace.tgz"},
        )

    @mcp.custom_route("/v1/analyze/jobs", methods=["GET"])
    async def analyze_jobs_list(request: Request) -> Response:
        from autorefactor.jobs import list_jobs

        limit_raw = request.query_params.get("limit")
        status = request.query_params.get("status")
        try:
            limit = int(limit_raw) if limit_raw is not None else 20
        except ValueError:
            limit = 20
        jobs = list_jobs(limit=limit, status=status)
        return _json({"ok": True, "jobs": jobs, "count": len(jobs)}, request=request)

    @mcp.custom_route("/v1/analyze/jobs/{job_id}", methods=["GET"])
    async def analyze_job_get(request: Request) -> Response:
        from autorefactor.jobs import job_status

        job_id = request.path_params.get("job_id") or ""
        return _json(job_status(job_id), request=request)

    @mcp.custom_route("/v1/analyze/jobs/{job_id}/cancel", methods=["POST", "OPTIONS"])
    async def analyze_job_cancel(request: Request) -> Response:
        if request.method == "OPTIONS":
            return _options(request, allow_mutations=True)
        from autorefactor.jobs import cancel_job

        job_id = request.path_params.get("job_id") or ""
        result = cancel_job(job_id)
        status = 200 if result.get("ok") else 400
        return _json(result, status_code=status, request=request, allow_mutations=True)

    @mcp.custom_route("/v1/status", methods=["GET"])
    async def status_get(request: Request) -> Response:
        workspace = request.query_params.get("workspace")
        out = request.query_params.get("out")
        payload = await run_in_threadpool(T.tool_get_status, workspace, out)
        return _json(payload, request=request)

    @mcp.custom_route("/v1/workspace/git", methods=["POST"])
    async def workspace_git(request: Request) -> Response:
        """Clone one or more remote git repositories into the active workspace."""
        from autorefactor.workspace_sync import clone_git_workspaces

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        git_url = (body or {}).get("git_url") or (body or {}).get("url")
        git_urls = (body or {}).get("git_urls") or []
        ref = (body or {}).get("ref")
        urls = [str(u) for u in git_urls if u]
        if git_url:
            urls = [str(git_url), *urls]
        if not urls:
            return JSONResponse({"ok": False, "error": "git_url or git_urls required"}, status_code=400)
        try:
            if len(urls) == 1:
                result = clone_git_workspace(urls[0], ref=str(ref) if ref else None)
            else:
                result = clone_git_workspaces(urls, ref=str(ref) if ref else None)
            return JSONResponse(result)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)[:500]}, status_code=400)

    # --- Report UI REST ---

    @mcp.custom_route("/v1/runs", methods=["GET", "OPTIONS"])
    async def runs_list(request: Request) -> Response:
        if request.method == "OPTIONS":
            return _options(request)
        return _json({"ok": True, "runs": runs_api.list_runs()}, request=request)

    @mcp.custom_route("/v1/runs/{slug}", methods=["GET", "DELETE", "OPTIONS"])
    async def runs_get(request: Request) -> Response:
        if request.method == "OPTIONS":
            return _options(request, allow_mutations=True)
        slug = request.path_params.get("slug") or ""
        if request.method == "DELETE":
            result = runs_api.delete_run(slug)
            status = 200 if result.get("ok") else 404 if result.get("error") == "not_found" else 400
            return _json(result, status_code=status, request=request, allow_mutations=True)
        summary = runs_api.run_summary(slug)
        if summary is None:
            return _json({"ok": False, "error": "not_found"}, status_code=404, request=request)
        return _json(summary, request=request)

    @mcp.custom_route("/v1/runs/{slug}/metrics", methods=["GET", "OPTIONS"])
    async def runs_metrics(request: Request) -> Response:
        if request.method == "OPTIONS":
            return _options(request)
        slug = request.path_params.get("slug") or ""
        data = runs_api.load_metrics_with_progress(slug)
        if data is None:
            return _json({"ok": False, "error": "not_found"}, status_code=404, request=request)
        return _json(data, request=request)

    @mcp.custom_route("/v1/runs/{slug}/report.md", methods=["GET", "OPTIONS"])
    async def runs_report_md(request: Request) -> Response:
        if request.method == "OPTIONS":
            return _options(request)
        slug = request.path_params.get("slug") or ""
        text = runs_api.load_run_text(slug, "report", "report.md")
        if text is None:
            return _json({"ok": False, "error": "not_found"}, status_code=404, request=request)
        headers = runs_api.cors_headers(request.headers.get("origin"))
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8", headers=headers)

    @mcp.custom_route("/v1/runs/{slug}/changesets", methods=["GET", "OPTIONS"])
    async def runs_changesets(request: Request) -> Response:
        if request.method == "OPTIONS":
            return _options(request)
        slug = request.path_params.get("slug") or ""
        if runs_api.resolve_run_dir(slug) is None:
            return _json({"ok": False, "error": "not_found"}, status_code=404, request=request)
        data = runs_api.load_changesets(slug) or []
        return _json({"ok": True, "changesets": data}, request=request)

    @mcp.custom_route(
        "/v1/runs/{slug}/changesets/{changeset_id}",
        methods=["PATCH", "OPTIONS"],
    )
    async def runs_changeset_decision(request: Request) -> Response:
        if request.method == "OPTIONS":
            return _options(request, allow_mutations=True)
        slug = request.path_params.get("slug") or ""
        changeset_id = request.path_params.get("changeset_id") or ""
        if runs_api.resolve_run_dir(slug) is None:
            return _json(
                {"ok": False, "error": "not_found"},
                status_code=404,
                request=request,
                allow_mutations=True,
            )
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _json(
                {"ok": False, "error": "invalid JSON"},
                status_code=400,
                request=request,
                allow_mutations=True,
            )
        if not isinstance(body, dict):
            return _json(
                {"ok": False, "error": "invalid JSON"},
                status_code=400,
                request=request,
                allow_mutations=True,
            )
        decision = body.get("decision")
        if decision is not None and not isinstance(decision, str):
            return _json(
                {"ok": False, "error": "decision must be string or null"},
                status_code=400,
                request=request,
                allow_mutations=True,
            )
        result = runs_api.apply_changeset_decision(slug, changeset_id, decision)
        status = 200 if result.get("ok") else (
            404 if result.get("error") in {"not_found", "changeset_not_found"} else 400
        )
        return _json(result, status_code=status, request=request, allow_mutations=True)

    @mcp.custom_route("/v1/runs/{slug}/units", methods=["GET", "OPTIONS"])
    async def runs_units(request: Request) -> Response:
        if request.method == "OPTIONS":
            return _options(request)
        slug = request.path_params.get("slug") or ""
        units = runs_api.load_units_light(slug)
        if units is None:
            return _json({"ok": False, "error": "not_found"}, status_code=404, request=request)
        return _json({"ok": True, "units": units, "count": len(units)}, request=request)

    @mcp.custom_route("/v1/runs/{slug}/projection", methods=["GET", "OPTIONS"])
    async def runs_projection(request: Request) -> Response:
        if request.method == "OPTIONS":
            return _options(request)
        slug = request.path_params.get("slug") or ""
        if runs_api.resolve_run_dir(slug) is None:
            return _json({"ok": False, "error": "not_found"}, status_code=404, request=request)
        data = runs_api.load_run_json(slug, "report", "projection.json")
        if data is None:
            return _json(
                {
                    "ok": False,
                    "error": "projection_missing",
                    "hint": "Re-run analyze to build 3D projection.json for this run.",
                },
                status_code=404,
                request=request,
            )
        return _json(data, request=request)

    @mcp.custom_route("/v1/runs/{slug}/graph", methods=["GET", "OPTIONS"])
    async def runs_graph(request: Request) -> Response:
        if request.method == "OPTIONS":
            return _options(request)
        slug = request.path_params.get("slug") or ""
        if runs_api.resolve_run_dir(slug) is None:
            return _json({"ok": False, "error": "not_found"}, status_code=404, request=request)
        data = runs_api.load_run_json(slug, "report", "graph.json")
        if data is None:
            return _json(
                {
                    "ok": False,
                    "error": "graph_missing",
                    "hint": "Re-run analyze to build graph.json for this run.",
                },
                status_code=404,
                request=request,
            )
        return _json(data, request=request)

    @mcp.custom_route("/v1/runs/{slug}/visuals/{name}", methods=["GET", "OPTIONS"])
    async def runs_visual(request: Request) -> Response:
        if request.method == "OPTIONS":
            return _options(request)
        slug = request.path_params.get("slug") or ""
        name = request.path_params.get("name") or ""
        path = runs_api.visual_path(slug, name)
        if path is None:
            return _json({"ok": False, "error": "not_found"}, status_code=404, request=request)
        headers = runs_api.cors_headers(request.headers.get("origin"))
        return FileResponse(path, media_type="image/png", headers=headers)

    @mcp.custom_route("/v1/config", methods=["GET", "PATCH", "OPTIONS"])
    async def config_route(request: Request) -> Response:
        if request.method == "OPTIONS":
            return _options(request, allow_mutations=True)
        if request.method == "GET":
            return _json(runs_api.get_config_payload(), request=request)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _json(
                {"ok": False, "error": "invalid JSON"},
                status_code=400,
                request=request,
                allow_mutations=True,
            )
        partial = body if isinstance(body, dict) else {}
        if "partial" in partial and isinstance(partial["partial"], dict):
            partial = partial["partial"]
        user_level = bool(partial.pop("user_level", True)) if isinstance(partial, dict) else True
        return _json(
            runs_api.patch_config(partial, user_level=user_level),
            request=request,
            allow_mutations=True,
        )

    @mcp.custom_route("/v1/templates", methods=["GET", "OPTIONS"])
    async def templates_route(request: Request) -> Response:
        if request.method == "OPTIONS":
            return _options(request)
        return _json(runs_api.list_templates(), request=request)

    @mcp.custom_route("/v1/runtime/detect", methods=["GET", "OPTIONS"])
    async def runtime_detect_route(request: Request) -> Response:
        if request.method == "OPTIONS":
            return _options(request)
        from autorefactor.runtime_status import runtime_detect_payload

        return _json(runtime_detect_payload(), request=request)

    @mcp.custom_route("/v1/analyze", methods=["POST", "OPTIONS"])
    async def analyze_start(request: Request) -> Response:
        if request.method == "OPTIONS":
            return _options(request, allow_mutations=True)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        body = body if isinstance(body, dict) else {}
        result = T.tool_analyze(
            workspace=body.get("workspace"),
            out=body.get("out"),
            skip_llm=bool(body.get("skip_llm", False)),
            describe_mode=body.get("describe_mode"),
            focus=body.get("focus"),
            background=bool(body.get("background", True)),
        )
        status = 200 if result.get("ok") or result.get("job_id") else 400
        return _json(result, status_code=status, request=request, allow_mutations=True)


def main() -> None:
    """Run MCP server (stdio or streamable-http) with workspace sync HTTP API."""
    transport = (os.environ.get("AUTOREFACTOR_MCP_TRANSPORT") or "stdio").strip().lower()
    host = os.environ.get("AUTOREFACTOR_MCP_HOST") or "127.0.0.1"
    port = int(os.environ.get("AUTOREFACTOR_MCP_PORT") or "8765")
    max_body = int(os.environ.get("AUTOREFACTOR_MCP_MAX_BODY") or str(512 * 1024 * 1024))

    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        import sys

        print(
            "autorefactor-mcp: requires MCP Python SDK v1 "
            '(pip install "mcp>=1.0.0,<2"). '
            f"Import failed: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    from autorefactor.mcp.guidance import format_mcp_instructions

    instructions = format_mcp_instructions()

    if transport in {"http", "streamable-http", "streamable_http", "sse"}:
        import inspect

        kwargs: dict[str, Any] = {
            "host": host,
            "port": port,
            "streamable_http_path": "/mcp",
            "sse_path": "/sse",
            "stateless_http": True,
            "instructions": instructions,
        }
        # Newer mcp builds accept max_request_body_size for large workspace syncs
        if "max_request_body_size" in inspect.signature(FastMCP.__init__).parameters:
            kwargs["max_request_body_size"] = max_body
        mcp = FastMCP("autorefactor", **kwargs)
        _register_tools(mcp)
        _register_http_routes(mcp)
        mode = "sse" if transport == "sse" else "streamable-http"
        print(
            f"autorefactor-mcp listening on http://{host}:{port}/mcp ({mode}); "
            f"sync POST http://{host}:{port}/v1/workspace/sync; "
            f"runs GET http://{host}:{port}/v1/runs; "
            f"export GET http://{host}:{port}/v1/workspace/export",
            flush=True,
        )
        mcp.run(transport=mode)
        return

    mcp = FastMCP("autorefactor", instructions=instructions)
    _register_tools(mcp)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
