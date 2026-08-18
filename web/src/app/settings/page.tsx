"use client";

import { useEffect, useState } from "react";
import {
  fetchConfig,
  fetchHealth,
  fetchRuntimeDetect,
  fetchTemplates,
  patchConfig,
  type RuntimeTemplate,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardDesc, CardTitle } from "@/components/ui/card";

const COMPOSE_OLLAMA = "docker compose --profile ollama up -d --build";
const COMPOSE_OLLAMA_GPU =
  "docker compose -f docker-compose.yml -f docker-compose.gpu.yml --profile ollama up -d --build";

function Tip({ text }: { text: string }) {
  return (
    <span className="ml-1 cursor-help text-muted" title={text} aria-label={text}>
      (?)
    </span>
  );
}

export default function SettingsPage() {
  const [config, setConfig] = useState<Record<string, unknown> | null>(null);
  const [templates, setTemplates] = useState<RuntimeTemplate[]>([]);
  const [chatBackend, setChatBackend] = useState("http");
  const [embedBackend, setEmbedBackend] = useState("http");
  const [embedDevice, setEmbedDevice] = useState("cpu");
  const [localChat, setLocalChat] = useState(false);
  const [describeMode, setDescribeMode] = useState("raw");
  const [llmModel, setLlmModel] = useState("");
  const [embedModel, setEmbedModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [detectNote, setDetectNote] = useState<string | null>(null);
  const [health, setHealth] = useState<{
    ollama_ok?: boolean | null;
    ollama_required?: boolean;
    compose_hint?: string | null;
  } | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const applyFromConfig = (c: Record<string, unknown>) => {
    setChatBackend(String(c.resolved_chat_backend || c.chat_backend || "http"));
    setEmbedBackend(String(c.resolved_embed_backend || c.embed_backend || "http"));
    setEmbedDevice(String(c.resolved_embed_device || c.embed_device || "cpu"));
    setLocalChat(Boolean(c.uses_local_chat || c.local_chat));
    setDescribeMode(String(c.describe_mode || "raw"));
    setLlmModel(String(c.llm_model || ""));
    setEmbedModel(String(c.embed_model || ""));
  };

  useEffect(() => {
    (async () => {
      try {
        const [c, t, h] = await Promise.all([
          fetchConfig(),
          fetchTemplates(),
          fetchHealth().catch(() => null),
        ]);
        setConfig(c.config);
        applyFromConfig(c.config);
        setTemplates(t.templates || []);
        if (h) setHealth(h);
      } catch (e) {
        setMsg(e instanceof Error ? e.message : "Failed to load config");
      }
    })();
  }, []);

  const applyTemplate = (t: RuntimeTemplate) => {
    if (t.chat_backend) setChatBackend(t.chat_backend);
    if (t.embed_backend) setEmbedBackend(t.embed_backend);
    if (t.embed_device) setEmbedDevice(t.embed_device);
    if (typeof t.local_chat === "boolean") setLocalChat(t.local_chat);
    if (t.describe_mode) setDescribeMode(t.describe_mode);
  };

  const detectBest = async () => {
    setBusy(true);
    setMsg(null);
    setDetectNote(null);
    try {
      const d = await fetchRuntimeDetect();
      const r = d.recommendation;
      if (r) {
        applyTemplate(r);
        setDetectNote(
          [r.rationale, d.cuda_hint, d.compose_hint].filter(Boolean).join(" "),
        );
      }
      const h = await fetchHealth().catch(() => null);
      if (h) setHealth(h);
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Detect failed");
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const partial: Record<string, unknown> = {
        chat_backend: chatBackend,
        embed_backend: embedBackend,
        embed_device: embedDevice,
        local_chat: localChat,
        describe_mode: describeMode,
        user_level: true,
      };
      if (llmModel.trim()) partial.llm_model = llmModel.trim();
      if (embedModel.trim()) partial.embed_model = embedModel.trim();
      if (apiKey.trim()) partial.api_key = apiKey.trim();
      const res = await patchConfig(partial);
      setConfig(res.config);
      applyFromConfig(res.config);
      setApiKey("");
      setMsg("Saved.");
      const h = await fetchHealth().catch(() => null);
      if (h) setHealth(h);
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Save failed");
    } finally {
      setBusy(false);
    }
  };

  const showOllamaHint =
    embedBackend === "local" &&
    (health?.ollama_ok === false ||
      (health?.ollama_required && health?.ollama_ok !== true));

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-4xl">Settings</h1>
        <p className="mt-2 max-w-2xl text-muted">
          Compose chat/embed backends and keys. This UI does not start or stop
          containers — use compose <code className="text-ink">--profile ollama</code>{" "}
          for local embeddings.
        </p>
      </div>

      <Card>
        <CardTitle>Templates</CardTitle>
        <CardDesc>Optional shortcuts that fill the axes below. Then Save.</CardDesc>
        <div className="mt-4 flex flex-wrap gap-2">
          {templates.map((t) => (
            <button
              key={t.id}
              type="button"
              className="rounded-md border border-line bg-paper px-3 py-1.5 text-sm hover:border-ink/40"
              onClick={() => applyTemplate(t)}
              title={t.description}
            >
              {t.label || t.id}
            </button>
          ))}
          <Button onClick={detectBest} disabled={busy} variant="outline">
            Detect best setup
          </Button>
        </div>
        {detectNote ? (
          <p className="mt-3 text-sm text-muted break-words">{detectNote}</p>
        ) : null}
      </Card>

      <Card>
        <CardTitle>Runtime axes</CardTitle>
        <CardDesc>Saved via PATCH /v1/config. Secrets show as set/unset.</CardDesc>
        <div className="mt-4 grid gap-4 sm:grid-cols-2">
          <label className="block text-sm">
            <span className="text-muted">
              Chat backend
              <Tip text="agent = host IDE model; http = OpenAI-compatible API; none = offline stubs" />
            </span>
            <select
              className="mt-1 w-full rounded-md border border-line bg-paper px-3 py-2"
              value={chatBackend}
              onChange={(e) => setChatBackend(e.target.value)}
            >
              <option value="agent">agent (host IDE)</option>
              <option value="http">http (OpenAI-compatible)</option>
              <option value="none">none</option>
            </select>
          </label>
          <label className="block text-sm">
            <span className="text-muted">
              Embed backend
              <Tip text="http = cloud embeddings (needs API key); local = Ollama; none = skip embed sensors" />
            </span>
            <select
              className="mt-1 w-full rounded-md border border-line bg-paper px-3 py-2"
              value={embedBackend}
              onChange={(e) => setEmbedBackend(e.target.value)}
            >
              <option value="local">local (Ollama)</option>
              <option value="http">http (cloud)</option>
              <option value="none">none</option>
            </select>
          </label>
          <label className="block text-sm">
            <span className="text-muted">
              Embed device (local)
              <Tip text="cpu uses nomic-embed-text; gpu uses qwen3-embedding when Ollama has CUDA" />
            </span>
            <select
              className="mt-1 w-full rounded-md border border-line bg-paper px-3 py-2"
              value={embedDevice}
              onChange={(e) => setEmbedDevice(e.target.value)}
              disabled={embedBackend !== "local"}
            >
              <option value="cpu">cpu (nomic-embed-text)</option>
              <option value="gpu">gpu (qwen3-embedding)</option>
            </select>
          </label>
          <label className="block text-sm">
            <span className="text-muted">
              Describe mode
              <Tip text="raw embeds source text; describe summarizes first (needs chat)" />
            </span>
            <select
              className="mt-1 w-full rounded-md border border-line bg-paper px-3 py-2"
              value={describeMode}
              onChange={(e) => setDescribeMode(e.target.value)}
            >
              <option value="raw">raw</option>
              <option value="describe">describe</option>
            </select>
          </label>
          <label className="flex items-center gap-2 text-sm sm:col-span-2">
            <input
              type="checkbox"
              checked={localChat}
              onChange={(e) => setLocalChat(e.target.checked)}
              disabled={chatBackend !== "http"}
            />
            <span className="text-muted">
              Route HTTP chat to Ollama (fully local chat)
              <Tip text="Requires Ollama chat model pull; prefer a GPU host" />
            </span>
          </label>
          <label className="block text-sm">
            <span className="text-muted">LLM model override</span>
            <input
              className="mt-1 w-full rounded-md border border-line bg-paper px-3 py-2"
              value={llmModel}
              onChange={(e) => setLlmModel(e.target.value)}
              placeholder="optional"
            />
          </label>
          <label className="block text-sm">
            <span className="text-muted">Embed model override</span>
            <input
              className="mt-1 w-full rounded-md border border-line bg-paper px-3 py-2"
              value={embedModel}
              onChange={(e) => setEmbedModel(e.target.value)}
              placeholder="optional"
            />
          </label>
          <label className="block text-sm sm:col-span-2">
            <span className="text-muted">
              API key
              <Tip text="Required for cloud chat/embed. Leave blank to keep the current value." />{" "}
              (optional; leave blank to keep {String(config?.api_key || "unset")})
            </span>
            <input
              type="password"
              className="mt-1 w-full rounded-md border border-line bg-paper px-3 py-2"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="sk-…"
            />
          </label>
        </div>
        <div className="mt-5 flex flex-wrap gap-3">
          <Button onClick={save} disabled={busy}>
            Save config
          </Button>
        </div>
        {msg ? <p className="mt-3 text-sm text-muted break-all">{msg}</p> : null}
      </Card>

      {showOllamaHint ? (
        <Card>
          <CardTitle>Ollama not reachable</CardTitle>
          <CardDesc>
            Local embeddings need the Ollama compose profile. Run from the repo root:
          </CardDesc>
          <pre className="mt-3 overflow-auto rounded-lg bg-paper p-3 text-xs text-muted">
            {COMPOSE_OLLAMA}
            {"\n"}
            {COMPOSE_OLLAMA_GPU}
          </pre>
        </Card>
      ) : null}

      {config ? (
        <Card>
          <CardTitle>Effective config (redacted)</CardTitle>
          <CardDesc>
            URLs and models after axis defaults. Ollama:{" "}
            {health?.ollama_ok === true
              ? "ok"
              : health?.ollama_ok === false
                ? "unreachable"
                : "n/a"}
          </CardDesc>
          <pre className="mt-3 max-h-80 overflow-auto rounded-lg bg-paper p-3 text-xs text-muted">
            {JSON.stringify(config, null, 2)}
          </pre>
        </Card>
      ) : null}
    </div>
  );
}
