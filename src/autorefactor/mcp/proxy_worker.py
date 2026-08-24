"""One-shot proxy worker — fresh interpreter so stdio MCP picks up src edits."""

from __future__ import annotations

import json
import sys


def main() -> None:
    from autorefactor.mcp.remote import proxy_tool_inprocess, reset_proxy_state

    raw = sys.stdin.read()
    payload = json.loads(raw) if raw.strip() else {}
    name = str(payload.get("name") or "")
    arguments = payload.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    reset_proxy_state()
    result = proxy_tool_inprocess(name, arguments)
    sys.stdout.write(json.dumps(result, default=str))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
