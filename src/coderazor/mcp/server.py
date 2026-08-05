from __future__ import annotations

import json
from typing import Any

from coderazor.mcp import tools as T


def _tool_list() -> list[dict[str, Any]]:
    return [
        {
            "name": "get_setup_requirements",
            "description": "Return missing coderazor config fields and questions for the user.",
            "inputSchema": {
                "type": "object",
                "properties": {"workspace": {"type": "string"}},
            },
        },
        {
            "name": "save_config",
            "description": "Persist coderazor settings to .coderazor.toml (or user config).",
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
            "description": "Run analysis pipeline; writes findings, report, and changeset queue under out/.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "workspace": {"type": "string"},
                    "out": {"type": "string"},
                    "skip_llm": {"type": "boolean"},
                    "describe_mode": {"type": "string"},
                },
                "required": ["workspace"],
            },
        },
        {
            "name": "get_report",
            "description": "Return report paths and metric highlights.",
            "inputSchema": {
                "type": "object",
                "properties": {"out": {"type": "string"}},
            },
        },
        {
            "name": "list_changesets",
            "description": "Compact list of changesets (id, title, confidence, status).",
            "inputSchema": {
                "type": "object",
                "properties": {"out": {"type": "string"}},
            },
        },
        {
            "name": "get_next_changeset",
            "description": "Fetch next pending changeset brief; may need user confirmation if low confidence.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "out": {"type": "string"},
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
                    "changeset_id": {"type": "string"},
                },
                "required": ["out", "changeset_id"],
            },
        },
        {
            "name": "mark_changeset",
            "description": "Update changeset status: pending|in_progress|done|skipped|blocked.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "out": {"type": "string"},
                    "changeset_id": {"type": "string"},
                    "status": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["out", "changeset_id", "status"],
            },
        },
        {
            "name": "describe_unit",
            "description": "On-demand LLM describe for one unit id.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "workspace": {"type": "string"},
                    "out": {"type": "string"},
                    "unit_id": {"type": "string"},
                    "skip_llm": {"type": "boolean"},
                },
                "required": ["workspace", "out", "unit_id"],
            },
        },
    ]


def dispatch(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "get_setup_requirements":
        return T.tool_get_setup_requirements(arguments.get("workspace"))
    if name == "save_config":
        return T.tool_save_config(arguments.get("partial") or {}, bool(arguments.get("user_level")))
    if name == "analyze":
        return T.tool_analyze(
            arguments["workspace"],
            arguments.get("out") or "out",
            bool(arguments.get("skip_llm")),
            arguments.get("describe_mode"),
        )
    if name == "get_report":
        return T.tool_get_report(arguments.get("out") or "out")
    if name == "list_changesets":
        return T.tool_list_changesets(arguments.get("out") or "out")
    if name == "get_next_changeset":
        return T.tool_get_next_changeset(
            arguments.get("out") or "out",
            arguments.get("min_confidence"),
        )
    if name == "get_changeset":
        return T.tool_get_changeset(arguments.get("out") or "out", arguments["changeset_id"])
    if name == "mark_changeset":
        return T.tool_mark_changeset(
            arguments.get("out") or "out",
            arguments["changeset_id"],
            arguments["status"],
            arguments.get("notes") or "",
        )
    if name == "describe_unit":
        return T.tool_describe_unit(
            arguments["workspace"],
            arguments.get("out") or "out",
            arguments["unit_id"],
            bool(arguments.get("skip_llm")),
        )
    return {"ok": False, "error": f"unknown tool {name}"}


def main() -> None:
    """Run MCP stdio server (Firebase-style CLI wrapper)."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        # Fallback minimal stdio JSON-RPC-ish loop for environments without FastMCP API
        import sys

        print(
            "coderazor-mcp: install `mcp` package. Tools available:\n"
            + json.dumps([t["name"] for t in _tool_list()]),
            file=sys.stderr,
        )
        # Still expose a simple line protocol for tests: TOOL name {json}
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            if line.startswith("TOOLS"):
                print(json.dumps(_tool_list()))
                continue
            if line.startswith("CALL "):
                _, name, payload = line.split(" ", 2)
                print(json.dumps(dispatch(name, json.loads(payload))))
                continue
            print(json.dumps({"error": "unknown command"}))
        return

    mcp = FastMCP("coderazor")

    @mcp.tool()
    def get_setup_requirements(workspace: str | None = None) -> dict:
        return T.tool_get_setup_requirements(workspace)

    @mcp.tool()
    def save_config(partial: dict, user_level: bool = False) -> dict:
        return T.tool_save_config(partial, user_level)

    @mcp.tool()
    def analyze(
        workspace: str,
        out: str = "out",
        skip_llm: bool = False,
        describe_mode: str | None = None,
    ) -> dict:
        return T.tool_analyze(workspace, out, skip_llm, describe_mode)

    @mcp.tool()
    def get_report(out: str = "out") -> dict:
        return T.tool_get_report(out)

    @mcp.tool()
    def list_changesets(out: str = "out") -> dict:
        return T.tool_list_changesets(out)

    @mcp.tool()
    def get_next_changeset(out: str = "out", min_confidence: float | None = None) -> dict:
        return T.tool_get_next_changeset(out, min_confidence)

    @mcp.tool()
    def get_changeset(out: str, changeset_id: str) -> dict:
        return T.tool_get_changeset(out, changeset_id)

    @mcp.tool()
    def mark_changeset(out: str, changeset_id: str, status: str, notes: str = "") -> dict:
        return T.tool_mark_changeset(out, changeset_id, status, notes)

    @mcp.tool()
    def describe_unit(workspace: str, out: str, unit_id: str, skip_llm: bool = False) -> dict:
        return T.tool_describe_unit(workspace, out, unit_id, skip_llm)

    mcp.run()


if __name__ == "__main__":
    main()
