import Link from "next/link";
import { fetchMetrics, fetchRun } from "@/lib/api";
import { DeleteRunButton } from "@/components/DeleteRunButton";
import {
  AgentEconomics,
  DebtBars,
  ProgressCompare,
  RoiBrief,
  Scoreboard,
  ValueQueue,
} from "@/components/roi/RoiPanels";
import { Card, CardDesc, CardTitle } from "@/components/ui/card";

export const dynamic = "force-dynamic";

export default async function RunRoiPage({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;
  let metrics: Awaited<ReturnType<typeof fetchMetrics>> | null = null;
  let summary: Awaited<ReturnType<typeof fetchRun>> | null = null;
  let error: string | null = null;
  try {
    summary = await fetchRun(slug);
    metrics = await fetchMetrics(slug);
  } catch (e) {
    error = e instanceof Error ? e.message : "Failed to load run";
  }

  if (error || !metrics) {
    return (
      <Card className="border-danger/40">
        <CardTitle>Run unavailable</CardTitle>
        <CardDesc>{error || "No metrics.json for this slug."}</CardDesc>
        <Link href="/runs" className="mt-3 inline-block text-accent hover:underline">
          Back to runs
        </Link>
      </Card>
    );
  }

  const title = summary?.display_name || summary?.label || slug;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="text-4xl">{title}</h1>
            {summary && typeof summary.fresh === "boolean" && !summary.fresh ? (
              <span className="rounded border border-danger/40 bg-danger-soft/50 px-2 py-0.5 text-xs uppercase tracking-wide text-danger">
                Stale
              </span>
            ) : null}
            {summary?.fresh === true ? (
              <span className="rounded border border-line px-2 py-0.5 text-xs uppercase tracking-wide text-muted">
                Fresh
              </span>
            ) : null}
          </div>
          <p className="mt-2 text-muted">
            {summary?.group ? (
              <>
                Group <span className="font-medium text-ink">{summary.group}</span>
                {" · "}
              </>
            ) : null}
            ROI story
            {summary && !summary.has_projection ? (
              <>
                {" "}
                <span className="text-danger">
                  No projection.json yet — re-run analyze (via MCP) for the 3D map.
                </span>
              </>
            ) : null}
          </p>
          <p className="mt-1 font-mono text-xs text-muted">{slug}</p>
          {summary?.freshness?.reason && summary.fresh === false ? (
            <p className="mt-1 text-xs text-muted">
              Freshness: {summary.freshness.reason}
            </p>
          ) : null}
        </div>
        <DeleteRunButton slug={slug} redirectTo="/runs" />
      </div>
      <RoiBrief metrics={metrics} slug={slug} />
      <ProgressCompare progress={metrics.progress} />
      <div className="grid gap-6 lg:grid-cols-2">
        <AgentEconomics metrics={metrics} />
        <DebtBars
          components={
            metrics.progress?.remaining?.debt_components ||
            metrics.roi?.debt_score?.components
          }
          title={
            metrics.progress?.counts?.done
              ? "Remaining technical debt"
              : "Technical debt snapshot"
          }
          description={
            metrics.progress?.counts?.done
              ? "Recomputed from open queue items (estimate)."
              : "Component scores are estimates (0–100)."
          }
        />
      </div>
      <div className="grid gap-6 lg:grid-cols-2">
        <ValueQueue rows={metrics.upside_by_changeset || []} slug={slug} />
        <Scoreboard metrics={metrics} />
      </div>
      {(metrics.quality_warnings || []).length ? (
        <Card>
          <CardTitle>Quality warnings</CardTitle>
          <ul className="mt-3 list-disc space-y-1 pl-5 text-sm text-muted">
            {metrics.quality_warnings!.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </Card>
      ) : null}
    </div>
  );
}
