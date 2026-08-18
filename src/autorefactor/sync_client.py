from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from pathlib import Path

from autorefactor.workspace_sync import extract_tar_gz_into, pack_workspace_tar_gz


def sync_workspace_http(
    workspace: Path,
    *,
    url: str = "http://127.0.0.1:8765/v1/workspace/sync",
    label: str | None = None,
    source: str | None = None,
    timeout: float = 600.0,
) -> dict:
    """Pack the working tree and POST it to a autorefactor MCP sync endpoint."""
    buf = io.BytesIO()
    meta = pack_workspace_tar_gz(workspace, buf)
    payload = buf.getvalue()
    q = []
    if label or workspace.name:
        from urllib.parse import quote

        q.append(f"label={quote(label or workspace.name)}")
    if source:
        from urllib.parse import quote

        q.append(f"source={quote(source)}")
    endpoint = url
    if q:
        endpoint = url + ("&" if "?" in url else "?") + "&".join(q)
    req = urllib.request.Request(
        endpoint,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/gzip",
            "Content-Length": str(len(payload)),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            result = json.loads(body) if body else {"ok": True}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"sync failed HTTP {exc.code}: {detail[:500]}") from exc
    result["uploaded_bytes"] = len(payload)
    result["pack"] = meta
    return result


def pull_workspace_http(
    dest: Path,
    *,
    url: str = "http://127.0.0.1:8765/v1/workspace/export",
    paths: list[str] | None = None,
    timeout: float = 600.0,
) -> dict:
    """Download the active workspace export and merge files into ``dest``."""
    from urllib.parse import quote

    endpoint = url
    if paths:
        endpoint = url + ("&" if "?" in url else "?") + "paths=" + quote(",".join(paths))
    req = urllib.request.Request(endpoint, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"export failed HTTP {exc.code}: {detail[:500]}") from exc
    if not data:
        raise RuntimeError("export returned empty body")
    written = extract_tar_gz_into(data, dest)
    return {
        "ok": True,
        "root": str(dest.resolve()),
        "pulled_paths": written,
        "docs_pulled": len(written),
        "downloaded_bytes": len(data),
    }
