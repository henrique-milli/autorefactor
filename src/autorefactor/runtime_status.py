from __future__ import annotations

import json
import shutil
import subprocess
import urllib.error
import urllib.request
from typing import Any

from autorefactor.config import AutorefactorConfig
from autorefactor.config_store import load_merged_config
from autorefactor.http.runs_api import RUNTIME_TEMPLATES


def probe_ollama(base_url: str, timeout: float = 2.0) -> bool:
    """True if an Ollama-compatible host responds."""
    root = (base_url or "").rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    for path in ("/api/tags", "/v1/models"):
        try:
            req = urllib.request.Request(f"{root}{path}", method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if 200 <= resp.status < 300:
                    return True
        except (urllib.error.URLError, TimeoutError, OSError):
            continue
    return False


def _ollama_roots(cfg: AutorefactorConfig) -> list[str]:
    roots: list[str] = []
    for candidate in (
        cfg.ollama_base_url,
        "http://ollama:11434/v1",
        "http://host.docker.internal:11434/v1",
        "http://127.0.0.1:11434/v1",
    ):
        root = (candidate or "").rstrip("/")
        if root and root not in roots:
            roots.append(root)
    return roots


def probe_ollama_any(cfg: AutorefactorConfig) -> tuple[bool, str | None]:
    for root in _ollama_roots(cfg):
        if probe_ollama(root):
            return True, root
    return False, None


def detect_nvidia() -> dict[str, Any]:
    """Best-effort nvidia-smi probe (often unavailable inside the engine container)."""
    if not shutil.which("nvidia-smi"):
        return {
            "available": False,
            "reason": "nvidia-smi not found in this environment",
            "gpus": [],
        }
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=5,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return {"available": False, "reason": str(exc), "gpus": []}
    gpus: list[dict[str, Any]] = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2:
            try:
                mem = float(parts[1])
            except ValueError:
                mem = None
            gpus.append({"name": parts[0], "vram_mb": mem})
    return {"available": bool(gpus), "reason": None, "gpus": gpus}


def ollama_cuda_hint(cfg: AutorefactorConfig, nvidia: dict[str, Any], ollama_ok: bool) -> str | None:
    if not ollama_ok:
        return None
    if not nvidia.get("available"):
        return None
    # Engine cannot see Ollama's GPU binding reliably; advise GPU overlay when NVIDIA is present.
    return (
        "NVIDIA GPU detected. If Ollama embeddings are slow, restart with: "
        "docker compose -f docker-compose.yml -f docker-compose.gpu.yml --profile ollama up -d --build"
    )


def recommend_axes(
    *,
    ollama_ok: bool,
    nvidia: dict[str, Any],
) -> dict[str, Any]:
    if not ollama_ok:
        tmpl = next(t for t in RUNTIME_TEMPLATES if t["id"] == "fast_cloud")
        return {
            **{k: tmpl[k] for k in (
                "chat_backend", "embed_backend", "embed_device", "local_chat", "describe_mode"
            )},
            "template_id": "fast_cloud",
            "rationale": "No Ollama reachable — recommend Fast cloud (paste an OpenAI-compatible API key).",
        }
    device = "cpu"
    vram_ok = False
    for gpu in nvidia.get("gpus") or []:
        mem = gpu.get("vram_mb")
        if isinstance(mem, (int, float)) and mem >= 4000:
            vram_ok = True
            break
    if nvidia.get("available") and vram_ok:
        device = "gpu"
    tmpl = next(t for t in RUNTIME_TEMPLATES if t["id"] == "local_agent")
    return {
        "chat_backend": tmpl["chat_backend"],
        "embed_backend": tmpl["embed_backend"],
        "embed_device": device,
        "local_chat": False,
        "describe_mode": "raw",
        "template_id": "local_agent",
        "rationale": (
            f"Ollama reachable — recommend Local agent with embed_device={device}."
        ),
    }


def runtime_detect_payload(config: AutorefactorConfig | None = None) -> dict[str, Any]:
    cfg = (config or load_merged_config()).apply_backend_defaults()
    nvidia = detect_nvidia()
    ollama_ok, ollama_url = probe_ollama_any(cfg)
    recommendation = recommend_axes(ollama_ok=ollama_ok, nvidia=nvidia)
    compose_hint = None
    if recommendation.get("embed_backend") == "local" and not ollama_ok:
        compose_hint = "docker compose --profile ollama up -d --build"
    elif recommendation.get("embed_device") == "gpu":
        compose_hint = (
            "docker compose -f docker-compose.yml -f docker-compose.gpu.yml "
            "--profile ollama up -d --build"
        )
    return {
        "ok": True,
        "nvidia": nvidia,
        "ollama_ok": ollama_ok,
        "ollama_url": ollama_url,
        "cuda_hint": ollama_cuda_hint(cfg, nvidia, ollama_ok),
        "recommendation": recommendation,
        "compose_hint": compose_hint,
    }


def runtime_health_payload(config: AutorefactorConfig | None = None) -> dict[str, Any]:
    cfg = (config or load_merged_config()).apply_backend_defaults()
    summary = cfg.runtime_summary()
    ollama_ok: bool | None = None
    ollama_required = bool(summary.get("ollama_required"))
    if ollama_required:
        ok, _url = probe_ollama_any(cfg)
        ollama_ok = ok
    return {
        "ok": True,
        "service": "autorefactor-mcp",
        "backends": {
            "chat": summary["chat_backend"],
            "embed": summary["embed_backend"],
        },
        "embed_device": summary.get("embed_device"),
        "local_chat": summary.get("local_chat"),
        "parallel_llm": summary["parallel_llm"],
        "describe_mode": summary.get("describe_mode"),
        "llm_model": summary["llm_model"],
        "embed_model": summary["embed_model"],
        "embed_batch_size": summary.get("embed_batch_size"),
        "embed_concurrency": summary.get("embed_concurrency"),
        "effective_embed_parallel": summary.get("effective_embed_parallel"),
        "ollama_required": ollama_required,
        "ollama_ok": ollama_ok,
        "profile": summary["profile"],
        "compose_hint": (
            "docker compose --profile ollama up -d --build"
            if ollama_required and ollama_ok is False
            else None
        ),
    }


def status_html(payload: dict[str, Any] | None = None) -> str:
    data = payload or runtime_health_payload()
    backends = data.get("backends") or {}
    ollama = data.get("ollama_ok")
    ollama_txt = "n/a" if ollama is None else ("ok" if ollama else "unreachable")
    mcp_snippet = json.dumps(
        {
            "mcpServers": {
                "autorefactor": {
                    "command": "autorefactor-mcp",
                    "env": {
                        "AUTOREFACTOR_REMOTE_URL": "http://127.0.0.1:8765",
                        "AUTOREFACTOR_MCP_TRANSPORT": "stdio",
                    },
                }
            }
        },
        indent=2,
    )
    hint = data.get("compose_hint") or ""
    hint_html = (
        f"<p><code>{hint}</code></p>"
        if hint
        else "<p>Paste an API key in Settings, or enable local Ollama with <code>--profile ollama</code>.</p>"
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>autorefactor</title>
  <style>
    :root {{
      --bg: #0f1419;
      --fg: #e7ecf1;
      --muted: #8b9aab;
      --accent: #3d9cf0;
      --card: #1a222c;
      --ok: #3ecf8e;
      --bad: #f07178;
    }}
    body {{
      margin: 0; font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
      background: radial-gradient(1200px 600px at 10% -10%, #1c2a3a, var(--bg));
      color: var(--fg); line-height: 1.45; min-height: 100vh;
    }}
    main {{ max-width: 720px; margin: 0 auto; padding: 3rem 1.25rem 4rem; }}
    h1 {{ font-size: 2rem; letter-spacing: -0.03em; margin: 0 0 0.35rem; }}
    .lead {{ color: var(--muted); margin: 0 0 2rem; }}
    .grid {{ display: grid; gap: 0.75rem; grid-template-columns: 1fr 1fr; }}
    .tile {{
      background: var(--card); border: 1px solid #2a3542; border-radius: 10px;
      padding: 0.9rem 1rem;
    }}
    .tile span {{ display: block; color: var(--muted); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.06em; }}
    .tile strong {{ font-size: 1.05rem; }}
    a {{ color: var(--accent); }}
    pre {{
      background: #0b1015; border: 1px solid #2a3542; border-radius: 10px;
      padding: 1rem; overflow: auto; font-size: 0.82rem;
    }}
    .ok {{ color: var(--ok); }} .bad {{ color: var(--bad); }}
    ol {{ color: var(--muted); }}
    ol strong {{ color: var(--fg); }}
  </style>
</head>
<body>
<main>
  <h1>autorefactor</h1>
  <p class="lead">Analysis engine status. Agent workflow is in MCP <code>instructions</code>; optional <code>get_agent_playbook</code> for live backend detail.</p>
  <div class="grid">
    <div class="tile"><span>Chat</span><strong>{backends.get("chat")}</strong></div>
    <div class="tile"><span>Embed</span><strong>{backends.get("embed")}</strong></div>
    <div class="tile"><span>Parallel LLM</span><strong>{"yes" if data.get("parallel_llm") else "no (agent sequential)"}</strong></div>
    <div class="tile"><span>LLM model</span><strong>{data.get("llm_model")}</strong></div>
    <div class="tile"><span>Embed model</span><strong>{data.get("embed_model")}</strong></div>
    <div class="tile"><span>Ollama</span><strong class="{"ok" if ollama else ("bad" if ollama is False else "")}">{ollama_txt}</strong></div>
    <div class="tile"><span>Sensor profile</span><strong>{data.get("profile")}</strong></div>
  </div>
  <h2>Quick start</h2>
  <ol>
    <li><strong>Cloud</strong> — <code>docker compose up -d --build</code>, paste OpenAI key in Settings.</li>
    <li><strong>Local agent</strong> — <code>docker compose --profile ollama up -d --build</code>, set chat=agent embed=local.</li>
  </ol>
  {hint_html}
  <h2>Cursor MCP (stdio proxy)</h2>
  <pre>{mcp_snippet}</pre>
  <p><a href="/health">/health</a> · <a href="/v1/status">/v1/status</a> · MCP at <code>/mcp</code></p>
</main>
</body>
</html>
"""
