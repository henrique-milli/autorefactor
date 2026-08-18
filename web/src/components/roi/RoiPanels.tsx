import Link from "next/link";
import type { Metrics, QueueProgress, UpsideRow } from "@/lib/api";
import { Card, CardDesc, CardTitle } from "@/components/ui/card";
import { KindBadge } from "@/components/ui/badge";

function num(v: unknown, fallback = 0) {
  return typeof v === "number" ? v : fallback;
}

function scoreOrUnavailable(v: unknown): string {
  return typeof v === "number" ? `${v.toFixed(0)}/100` : "unavailable";
}

export function RoiBrief({
  metrics,
  slug,
}: {
  metrics: Metrics;
  slug: string;
}) {
  const roi = metrics.roi;
  const hasRoi = Boolean(roi?.debt_score || roi?.agent_friction);
  const debtVal = roi?.debt_score?.value;
  const frictionVal = roi?.agent_friction?.value;
  const friction = typeof frictionVal === "number" ? frictionVal : null;
  const impact = metrics.impact || {};
  const estLoc = num(impact.estimated_loc_removed);
  const baseline = num(metrics.exact?.loc, 1);
  const locTiny = estLoc < 0.01 * baseline;
  const avgGain = metrics.roi?.per_task_gains?.averages?.gain_tokens;
  const top = (metrics.upside_by_changeset || []).slice(0, 3);

  return (
    <Card className="border-accent/20 bg-gradient-to-br from-card to-accent-soft/40">
      <CardTitle>Executive brief</CardTitle>
      <CardDesc>
        Soft metrics are estimates. Removable LOC is the dedup track only
        {locTiny
          ? " — here it’s small, so debt, friction, and per-task agent gains matter more."
          : "."}
        {!hasRoi ? " This run has no ROI block yet — re-run analyze." : ""}
      </CardDesc>
      <div className="mt-4 flex flex-wrap gap-6">
        <Stat label="Debt (est.)" value={scoreOrUnavailable(debtVal)} muted={typeof debtVal !== "number"} />
        <Stat
          label="Agent friction (est.)"
          value={scoreOrUnavailable(frictionVal)}
          danger={friction !== null && friction >= 50}
          muted={friction === null}
        />
        <Stat
          label="LOC dedup upside (est.)"
          value={`~${estLoc}`}
          muted={locTiny}
        />
        {typeof avgGain === "number" ? (
          <Stat label="Avg gain / task (est.)" value={`~${avgGain.toLocaleString()} tok-eq`} />
        ) : null}
      </div>
      {top.length > 0 ? (
        <ul className="mt-5 space-y-2 text-sm">
          {top.map((row) => (
            <li key={row.id} className="flex flex-wrap items-start gap-2">
              <KindBadge kind={row.upside_kind} />
              <span>
                {row.plain_english || row.upside || row.title}{" "}
                <span className="text-muted">
                  ({row.action}, value {(row.value_score || 0).toFixed(2)})
                </span>
              </span>
            </li>
          ))}
        </ul>
      ) : null}
      <div className="mt-5">
        <Link href={`/runs/${slug}/map`} className="text-sm font-medium text-accent hover:underline">
          Open interactive vector map →
        </Link>
      </div>
    </Card>
  );
}

function Stat({
  label,
  value,
  danger,
  muted,
}: {
  label: string;
  value: string;
  danger?: boolean;
  muted?: boolean;
}) {
  return (
    <div>
      <div className="text-xs uppercase tracking-wide text-muted">{label}</div>
      <div
        className={`mt-1 text-2xl font-semibold ${
          danger ? "text-danger" : muted ? "text-muted text-base" : "text-ink"
        }`}
      >
        {value}
      </div>
    </div>
  );
}

export function ProgressCompare({ progress }: { progress?: QueueProgress }) {
  if (!progress?.at_analyze) {
    return null;
  }
  const a = progress.at_analyze;
  const r = progress.realized || {};
  const rem = progress.remaining || {};
  const counts = progress.counts || {};

  const rows: { label: string; analyze: string; realized: string; remaining: string }[] = [
    {
      label: "Debt (est.)",
      analyze: scoreCell(a.debt_score),
      realized: dropCell(r.debt_score_drop),
      remaining: scoreCell(rem.debt_score),
    },
    {
      label: "Agent friction (est.)",
      analyze: scoreCell(a.agent_friction),
      realized: dropCell(r.agent_friction_drop),
      remaining: scoreCell(rem.agent_friction),
    },
    {
      label: "LOC remove (est.)",
      analyze: locCell(a.estimated_loc_removed),
      realized: locCell(r.estimated_loc_removed),
      remaining: locCell(rem.estimated_loc_removed),
    },
    {
      label: "LOC under review",
      analyze: locCell(a.loc_under_review),
      realized: locCell(r.loc_under_review),
      remaining: locCell(rem.loc_under_review),
    },
    {
      label: "Tasks",
      analyze: String(a.tasks ?? "—"),
      realized: String(r.tasks ?? 0),
      remaining: String(rem.tasks ?? 0),
    },
  ];

  return (
    <Card className="border-accent/25">
      <CardTitle>Before / after (queue progress)</CardTitle>
      <CardDesc>
        Analyze snapshot vs estimated benefits from done items. Remaining debt/friction are
        recomputed from the open queue — still estimates, not a re-analyze.
        {typeof counts.done === "number" ? (
          <>
            {" "}
            Done {counts.done}/{counts.total ?? "?"}
            {counts.skipped ? ` · skipped ${counts.skipped}` : ""}
            {counts.blocked ? ` · blocked ${counts.blocked}` : ""}.
          </>
        ) : null}
      </CardDesc>
      <div className="mt-4 overflow-x-auto">
        <table className="w-full min-w-[28rem] text-sm">
          <thead className="text-left text-xs uppercase tracking-wide text-muted">
            <tr>
              <th className="py-2 pr-3 font-medium">Metric</th>
              <th className="py-2 pr-3 font-medium">At analyze</th>
              <th className="py-2 pr-3 font-medium">Realized (done)</th>
              <th className="py-2 font-medium">Remaining</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.label} className="border-t border-line/60">
                <td className="py-2.5 pr-3 text-muted">{row.label}</td>
                <td className="py-2.5 pr-3 font-semibold tabular-nums">{row.analyze}</td>
                <td className="py-2.5 pr-3 font-semibold tabular-nums text-accent">
                  {row.realized}
                </td>
                <td className="py-2.5 font-semibold tabular-nums">{row.remaining}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function scoreCell(v: unknown): string {
  return typeof v === "number" ? `${v.toFixed(0)}/100` : "—";
}

function dropCell(v: unknown): string {
  return typeof v === "number" && v > 0 ? `−${v.toFixed(0)}` : typeof v === "number" ? "0" : "—";
}

function locCell(v: unknown): string {
  return typeof v === "number" ? `~${v}` : "—";
}

export function DebtBars({
  components,
  title = "Technical debt snapshot",
  description = "Component scores are estimates (0–100).",
}: {
  components?: Record<string, number>;
  title?: string;
  description?: string;
}) {
  const order = ["complexity", "clone", "coverage", "docs", "slop"];
  const entries = order
    .filter((k) => components && k in components)
    .map((k) => [k, components![k]] as const);
  if (!entries.length) {
    return (
      <Card>
        <CardTitle>Technical debt</CardTitle>
        <CardDesc>No debt components in this run.</CardDesc>
      </Card>
    );
  }
  return (
    <Card>
      <CardTitle>{title}</CardTitle>
      <CardDesc>{description}</CardDesc>
      <div className="mt-4 space-y-3">
        {entries.map(([k, v]) => (
          <div key={k}>
            <div className="mb-1 flex justify-between text-xs text-muted">
              <span className="capitalize">{k}</span>
              <span>{Number(v).toFixed(0)}</span>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-line/60">
              <div
                className="h-full rounded-full bg-danger/80"
                style={{ width: `${Math.min(100, Number(v))}%` }}
              />
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}

export function AgentEconomics({ metrics }: { metrics: Metrics }) {
  const roi = metrics.roi || {};
  const blast = num(roi.context_tokens_blast_radius?.value);
  const dedup = num(roi.context_tokens_dedup_saved?.value);
  const band = String(roi.attempt_savings_band?.value || "none");
  const per = roi.per_task_gains;
  const avg = per?.averages;
  const top = (per?.tasks || []).slice(0, 5);
  const tasksN = num(avg?.tasks, top.length);

  return (
    <Card>
      <CardTitle>Agent economics</CardTitle>
      <CardDesc>
        Estimated gains <span className="font-medium text-ink">per task</span>, then portfolio
        totals. Dedup tokens, clearer blast radius, and fewer retry loops.
      </CardDesc>
      {avg ? (
        <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-4">
          <div>
            <dt className="text-muted">Avg gain / task</dt>
            <dd className="text-xl font-semibold">
              ~{num(avg.gain_tokens).toLocaleString()}
              <span className="text-sm font-normal text-muted"> tok-eq</span>
            </dd>
          </div>
          <div>
            <dt className="text-muted">Avg dedup save</dt>
            <dd className="text-xl font-semibold">
              ~{num(avg.dedup_tokens_saved).toLocaleString()}
              <span className="text-sm font-normal text-muted"> tok</span>
            </dd>
          </div>
          <div>
            <dt className="text-muted">Avg retries avoided</dt>
            <dd className="text-xl font-semibold">~{num(avg.retries_avoided).toFixed(1)}</dd>
          </div>
          <div>
            <dt className="text-muted">Tasks</dt>
            <dd className="text-xl font-semibold">{tasksN}</dd>
          </div>
        </dl>
      ) : (
        <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-3">
          <div>
            <dt className="text-muted">Blast radius (est.)</dt>
            <dd className="text-xl font-semibold">~{blast.toLocaleString()} tok</dd>
          </div>
          <div>
            <dt className="text-muted">Dedup save (est.)</dt>
            <dd className="text-xl font-semibold">~{dedup.toLocaleString()} tok</dd>
          </div>
          <div>
            <dt className="text-muted">Attempt-savings band</dt>
            <dd className="text-xl font-semibold capitalize">{band}</dd>
          </div>
        </dl>
      )}
      {top.length ? (
        <div className="mt-5 overflow-x-auto">
          <table className="w-full min-w-[24rem] text-sm">
            <thead className="text-left text-xs uppercase tracking-wide text-muted">
              <tr>
                <th className="py-2 pr-2 font-medium">Task</th>
                <th className="py-2 pr-2 font-medium">Gain</th>
                <th className="py-2 pr-2 font-medium">Dedup</th>
                <th className="py-2 font-medium">Retries</th>
              </tr>
            </thead>
            <tbody>
              {top.map((t) => (
                <tr key={t.id} className="border-t border-line/60 align-top">
                  <td className="py-2 pr-2">
                    <div className="font-medium leading-snug">{t.title || t.id}</div>
                    <div className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[11px] text-muted">
                      <KindBadge kind={t.upside_kind} />
                      <code>{t.action}</code>
                    </div>
                  </td>
                  <td className="py-2 pr-2 font-semibold tabular-nums text-accent">
                    ~{num(t.gain_tokens).toLocaleString()}
                  </td>
                  <td className="py-2 pr-2 tabular-nums text-muted">
                    ~{num(t.dedup_tokens_saved).toLocaleString()}
                  </td>
                  <td className="py-2 tabular-nums">~{num(t.retries_avoided).toFixed(1)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {avg ? (
        <p className="mt-4 text-xs text-muted">
          Portfolio: ~{blast.toLocaleString()} tok blast · ~{dedup.toLocaleString()} tok dedup ·
          attempt-savings band <span className="capitalize">{band}</span>
        </p>
      ) : null}
    </Card>
  );
}

export function ValueQueue({
  rows,
  slug,
  limit = 12,
}: {
  rows: UpsideRow[];
  slug: string;
  limit?: number;
}) {
  const top = rows.slice(0, limit);
  return (
    <Card>
      <CardTitle>Top opportunities by value</CardTitle>
      <CardDesc>Ranked by value_score — not confidence alone.</CardDesc>
      <ol className="mt-4 space-y-3">
        {top.map((row, i) => (
          <li key={row.id} className="border-b border-line/70 pb-3 last:border-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs text-muted">{i + 1}.</span>
              <KindBadge kind={row.upside_kind} />
              <code className="text-xs text-muted">{row.action}</code>
              <span className="text-xs text-muted">
                conf {(row.confidence || 0).toFixed(2)} · value{" "}
                {(row.value_score || 0).toFixed(2)}
              </span>
            </div>
            <p className="mt-1 text-sm">{row.plain_english || row.upside || row.title}</p>
            <Link
              href={`/runs/${slug}/map?cs=${encodeURIComponent(row.id)}`}
              className="mt-1 inline-block text-xs text-accent hover:underline"
            >
              Focus on map
            </Link>
          </li>
        ))}
        {!top.length ? <li className="text-sm text-muted">Empty queue.</li> : null}
      </ol>
    </Card>
  );
}

export function Scoreboard({ metrics }: { metrics: Metrics }) {
  const impact = metrics.impact || {};
  const rows: [string, string][] = [
    ["Ranked tasks", String(impact.agent_ready_changesets ?? "—")],
    ["High-confidence", String(impact.high_confidence_changesets ?? "—")],
    ["Est. LOC remove (est.)", String(impact.estimated_loc_removed ?? "—")],
    ["LOC under review", String(impact.loc_under_review ?? "—")],
    ["Cross-repo", String(impact.cross_repo_opportunities ?? "—")],
  ];
  return (
    <Card>
      <CardTitle>Scoreboard</CardTitle>
      <table className="mt-3 w-full text-sm">
        <tbody>
          {rows.map(([k, v]) => (
            <tr key={k} className="border-b border-line/50">
              <td className="py-2 text-muted">{k}</td>
              <td className="py-2 text-right font-semibold">{v}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}
