#!/usr/bin/env python3
"""Probe Streamable HTTP MCP at localhost:8765 — latency + tool smoke."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8765/mcp"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def rpc(method: str, params: dict | None = None, *, req_id: int = 1, session: str | None = None) -> tuple[float, dict | list | str, dict]:
    body = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    headers = dict(HEADERS)
    if session:
        headers["Mcp-Session-Id"] = session
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE, data=data, headers=headers, method="POST")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            raw = resp.read().decode()
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            elapsed = time.perf_counter() - t0
    except urllib.error.HTTPError as e:
        elapsed = time.perf_counter() - t0
        raw = e.read().decode(errors="replace")
        resp_headers = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
        return elapsed, {"http_error": e.code, "body": raw[:2000]}, resp_headers

    # Streamable HTTP may return SSE or plain JSON
    if raw.startswith("event:") or "data:" in raw[:80]:
        payloads = []
        for line in raw.splitlines():
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if chunk:
                    try:
                        payloads.append(json.loads(chunk))
                    except json.JSONDecodeError:
                        payloads.append(chunk)
        return elapsed, payloads[-1] if payloads else raw, resp_headers
    try:
        return elapsed, json.loads(raw), resp_headers
    except json.JSONDecodeError:
        return elapsed, raw, resp_headers


def main() -> None:
    report: dict = {"endpoint": BASE, "steps": []}

    t, init, hdrs = rpc(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "autorefactor-eval", "version": "0.3.0"},
        },
        req_id=1,
    )
    session = hdrs.get("mcp-session-id")
    report["steps"].append({"step": "initialize", "seconds": round(t, 3), "session": bool(session), "result": init})

    # notifications/initialized (no id)
    body = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    headers = dict(HEADERS)
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(BASE, data=json.dumps(body).encode(), headers=headers, method="POST")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        notify_t = time.perf_counter() - t0
    except urllib.error.HTTPError as e:
        e.read()
        notify_t = time.perf_counter() - t0
    report["steps"].append({"step": "notifications/initialized", "seconds": round(notify_t, 3)})

    t, tools, _ = rpc("tools/list", {}, req_id=2, session=session)
    tool_names = []
    if isinstance(tools, dict):
        result = tools.get("result") or tools
        tool_names = [x.get("name") for x in (result.get("tools") or []) if isinstance(x, dict)]
    report["steps"].append({"step": "tools/list", "seconds": round(t, 3), "tools": tool_names})

    # Analyze fixture workspace inside container mount (repo root → fixtures/demo-workspace)
    t, analyze, _ = rpc(
        "tools/call",
        {
            "name": "analyze",
            "arguments": {
                "workspace": "/workspace/fixtures/demo-workspace",
                "skip_llm": True,
            },
        },
        req_id=3,
        session=session,
    )
    analyze_summary = analyze
    if isinstance(analyze, dict):
        content = (analyze.get("result") or {}).get("content") or analyze.get("content")
        if content and isinstance(content, list) and content:
            text = content[0].get("text") if isinstance(content[0], dict) else content[0]
            try:
                analyze_summary = json.loads(text) if isinstance(text, str) else text
            except (json.JSONDecodeError, TypeError):
                analyze_summary = text
        elif "result" in analyze:
            analyze_summary = analyze["result"]
    report["steps"].append({"step": "tools/call analyze", "seconds": round(t, 3), "result": analyze_summary})

    t, report_tool, _ = rpc(
        "tools/call",
        {
            "name": "get_report",
            "arguments": {"workspace": "/workspace/fixtures/demo-workspace"},
        },
        req_id=4,
        session=session,
    )
    report_summary = report_tool
    if isinstance(report_tool, dict):
        content = (report_tool.get("result") or {}).get("content") or report_tool.get("content")
        if content and isinstance(content, list) and content:
            text = content[0].get("text") if isinstance(content[0], dict) else content[0]
            try:
                report_summary = json.loads(text) if isinstance(text, str) else text
            except (json.JSONDecodeError, TypeError):
                report_summary = text
    report["steps"].append({"step": "tools/call get_report", "seconds": round(t, 3), "result": report_summary})

    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
