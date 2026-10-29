export type RunSummary = {
  slug: string;
  display_name?: string;
  label?: string;
  group?: string;
  source?: string | null;
  repos?: string[];
  path: string;
  mtime: number;
  has_report: boolean;
  has_projection: boolean;
  changeset_count: number;
  debt_score?: number | null;
  agent_friction?: number | null;
  fresh?: boolean;
  freshness?: {
    run_fingerprint?: string | null;
    current_fingerprint?: string | null;
    reason?: string;
    git?: { available?: boolean; head?: string | null; dirty?: boolean | null };
  };
};

export type QueueProgress = {
  method?: string;
  counts?: {
    total?: number;
    done?: number;
    open?: number;
    skipped?: number;
    blocked?: number;
  };
  at_analyze?: {
    debt_score?: number | null;
    agent_friction?: number | null;
    estimated_loc_removed?: number;
    loc_under_review?: number;
    tasks?: number;
    debt_components?: Record<string, number> | null;
  };
  realized?: {
    debt_score_drop?: number | null;
    agent_friction_drop?: number | null;
    estimated_loc_removed?: number;
    loc_under_review?: number;
    tasks?: number;
  };
  remaining?: {
    debt_score?: number | null;
    agent_friction?: number | null;
    estimated_loc_removed?: number;
    loc_under_review?: number;
    tasks?: number;
    debt_components?: Record<string, number> | null;
  };
};

export type PerTaskGain = {
  id: string;
  title?: string;
  action?: string;
  confidence?: number;
  gain_tokens?: number;
  blast_tokens?: number;
  dedup_tokens_saved?: number;
  retries_avoided?: number;
  upside_kind?: string;
};

export type Metrics = {
  exact?: Record<string, unknown>;
  impact?: Record<string, unknown>;
  progress?: QueueProgress;
  confidence_threshold?: number;
  roi?: {
    debt_score?: {
      value?: number;
      label?: string;
      method?: string;
      components?: Record<string, number>;
    };
    agent_friction?: { value?: number; label?: string; method?: string };
    context_tokens_blast_radius?: { value?: number; label?: string };
    context_tokens_dedup_saved?: { value?: number; label?: string };
    attempt_savings_band?: { value?: string; method?: string };
    per_task_gains?: {
      label?: string;
      method?: string;
      averages?: {
        gain_tokens?: number;
        blast_tokens?: number;
        dedup_tokens_saved?: number;
        retries_avoided?: number;
        tasks?: number;
      };
      tasks?: PerTaskGain[];
      top_ids?: string[];
    };
    value_ranked_changeset_ids?: string[];
  };
  upside_by_changeset?: UpsideRow[];
  quality_warnings?: string[];
};

export type UpsideRow = {
  id: string;
  title: string;
  action: string;
  confidence: number;
  estimated_loc_removed?: number;
  loc_under_review?: number;
  value_score?: number;
  upside_kind?: string;
  plain_english?: string;
  upside?: string;
  cross_repo?: boolean;
  repos?: string[];
};

export type ProjectionPoint = {
  unit_id: string;
  repo: string;
  path: string;
  qualified_name: string;
  kind: string;
  x: number;
  y: number;
  z: number;
  on_queue: boolean;
  changeset_ids: string[];
  upside_kind?: string | null;
};

export type GraphEdge = {
  source: string;
  target: string;
  changeset_id: string;
  action: string;
  kind: string;
};

export type ChangesetTarget = {
  repo?: string;
  path?: string;
  symbol?: string;
  unit_id?: string | null;
  source_snippet?: string;
};

export type ChangesetLite = {
  id: string;
  title: string;
  action: string;
  brief?: string;
  rationale?: string;
  /** One-sentence model reason for the flag (prefer over brief). */
  why?: string;
  /** Expected benefit / upside (may differ from why). */
  benefit?: string;
  confidence?: number;
  status?: string;
  needs_review?: boolean;
  hitl_reason?: string | null;
  finding_kind?: string | null;
  upside_kind?: string | null;
  /** UI decide-now: proceed | skip | blocked */
  user_decision?: string | null;
  /** True when this is a real multi-target dedup/clone comparison. */
  compare_targets?: boolean;
  metrics_delta?: Record<string, unknown>;
  targets?: ChangesetTarget[];
};

export type UnitLite = {
  id: string;
  repo?: string;
  path?: string;
  kind?: string;
  qualified_name?: string;
  span?: { start_line?: number; end_line?: number };
  source_snippet?: string;
};

const BASE =
  typeof window === "undefined"
    ? process.env.AUTOREFACTOR_API_URL || "http://127.0.0.1:8765"
    : "/api/backend";

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { cache: "no-store" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw Object.assign(new Error((body as { error?: string }).error || res.statusText), {
      status: res.status,
      body,
    });
  }
  return res.json() as Promise<T>;
}

export async function fetchRuns(): Promise<RunSummary[]> {
  const data = await getJson<{ runs: RunSummary[] }>("/v1/runs");
  return data.runs || [];
}

export async function fetchRun(slug: string) {
  return getJson<RunSummary & { ok?: boolean; unit_count?: number }>(
    `/v1/runs/${encodeURIComponent(slug)}`,
  );
}

export async function fetchMetrics(slug: string): Promise<Metrics> {
  return getJson<Metrics>(`/v1/runs/${encodeURIComponent(slug)}/metrics`);
}

export async function fetchChangesets(slug: string): Promise<ChangesetLite[]> {
  const data = await getJson<{ changesets: ChangesetLite[] }>(
    `/v1/runs/${encodeURIComponent(slug)}/changesets`,
  );
  return data.changesets || [];
}

export async function fetchUnits(slug: string): Promise<UnitLite[]> {
  const data = await getJson<{ units: UnitLite[] }>(
    `/v1/runs/${encodeURIComponent(slug)}/units`,
  );
  return data.units || [];
}

export async function fetchProjection(slug: string) {
  return getJson<{ points: ProjectionPoint[]; point_count: number }>(
    `/v1/runs/${encodeURIComponent(slug)}/projection`,
  );
}

export async function fetchGraph(slug: string) {
  return getJson<{ edges: GraphEdge[]; edge_count: number }>(
    `/v1/runs/${encodeURIComponent(slug)}/graph`,
  );
}

export async function fetchConfig() {
  return getJson<{ ok: boolean; config: Record<string, unknown> }>("/v1/config");
}

export async function patchConfig(partial: Record<string, unknown>) {
  const res = await fetch(`${BASE}/v1/config`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(partial),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function patchChangesetDecision(
  slug: string,
  changesetId: string,
  decision: "proceed" | "skip" | "blocked" | null,
) {
  const res = await fetch(
    `${BASE}/v1/runs/${encodeURIComponent(slug)}/changesets/${encodeURIComponent(changesetId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision }),
    },
  );
  const body = (await res.json().catch(() => ({}))) as {
    ok?: boolean;
    error?: string;
    changeset?: ChangesetLite & {
      user_decision?: string | null;
      needs_review?: boolean;
    };
  };
  if (!res.ok || !body.ok) {
    throw new Error(body.error || res.statusText || "Failed to save decision");
  }
  return body;
}

export async function fetchTemplates() {
  return getJson<{ templates: RuntimeTemplate[] }>("/v1/templates");
}

export type RuntimeTemplate = {
  id: string;
  label?: string;
  description: string;
  chat_backend?: string;
  embed_backend?: string;
  embed_device?: string;
  local_chat?: boolean;
  describe_mode?: string;
};

export async function fetchHealth() {
  return getJson<{
    ok: boolean;
    backends?: { chat?: string; embed?: string };
    ollama_ok?: boolean | null;
    ollama_required?: boolean;
    hint?: string | null;
    llm_model?: string;
    embed_model?: string;
  }>("/health");
}

export async function fetchRuntimeDetect() {
  return getJson<{
    ok: boolean;
    nvidia?: { available?: boolean; reason?: string | null; gpus?: { name?: string; vram_mb?: number }[] };
    ollama_ok?: boolean;
    ollama_url?: string | null;
    cuda_hint?: string | null;
    recommendation?: RuntimeTemplate & { template_id?: string; rationale?: string };
    hint?: string | null;
  }>("/v1/runtime/detect");
}

export async function fetchJobs(limit = 20) {
  return getJson<{ jobs: Record<string, unknown>[]; count: number }>(
    `/v1/analyze/jobs?limit=${limit}`,
  );
}

export async function cancelJob(jobId: string) {
  const res = await fetch(`${BASE}/v1/analyze/jobs/${encodeURIComponent(jobId)}/cancel`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
  return res.json() as Promise<Record<string, unknown>>;
}

export async function deleteRun(slug: string) {
  const res = await fetch(`${BASE}/v1/runs/${encodeURIComponent(slug)}`, {
    method: "DELETE",
  });
  return res.json() as Promise<{
    ok: boolean;
    deleted_run?: string;
    deleted_dir?: boolean;
    deleted_jobs?: string[];
    deleted_tmp?: string[];
    error?: string;
  }>;
}
