import Link from "next/link";
import { fetchRuns, type RunSummary } from "@/lib/api";
import { DeleteRunButton } from "@/components/DeleteRunButton";
import { Card, CardDesc, CardTitle } from "@/components/ui/card";

export const dynamic = "force-dynamic";

export default async function RunsPage() {
  let runs: RunSummary[] = [];
  let error: string | null = null;
  try {
    runs = await fetchRuns();
  } catch (e) {
    error = e instanceof Error ? e.message : "Failed to load runs";
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-4xl text-ink">Runs</h1>
        <p className="mt-2 max-w-2xl text-muted">
          One run per synced project (re-analyze overwrites). Titles use the
          project name. Analyze via MCP/agent; delete here when cleaning up.
        </p>
      </div>
      {error ? (
        <Card className="border-danger/40 bg-danger-soft/40">
          <CardTitle>API unreachable</CardTitle>
          <CardDesc>
            Start the Python MCP HTTP server on :8765 (or set AUTOREFACTOR_API_URL). {error}
          </CardDesc>
        </Card>
      ) : null}
      {runs.length ? (
        <div className="overflow-hidden rounded-xl border border-line bg-card">
          <table className="w-full text-sm">
            <thead className="bg-paper/80 text-left text-xs uppercase tracking-wide text-muted">
              <tr>
                <th className="px-4 py-3">Project</th>
                <th className="px-4 py-3">Debt</th>
                <th className="px-4 py-3">Friction</th>
                <th className="px-4 py-3">Queue</th>
                <th className="px-4 py-3">Updated</th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.slug} className="border-t border-line/70 hover:bg-accent-soft/30">
                  <td className="px-4 py-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <Link
                        href={`/runs/${r.slug}`}
                        className="font-medium text-accent hover:underline"
                      >
                        {r.display_name || r.label || r.slug}
                      </Link>
                      {typeof r.fresh === "boolean" && !r.fresh ? (
                        <span className="rounded border border-danger/40 bg-danger-soft/50 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-danger">
                          Stale
                        </span>
                      ) : null}
                      {r.fresh === true ? (
                        <span className="rounded border border-line px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted">
                          Fresh
                        </span>
                      ) : null}
                    </div>
                    <div className="mt-0.5 font-mono text-[11px] text-muted">{r.slug}</div>
                    {r.source ? (
                      <div className="truncate text-[11px] text-muted" title={r.source}>
                        {r.source}
                      </div>
                    ) : null}
                    {!r.has_projection ? (
                      <span className="text-xs text-muted">no 3D yet</span>
                    ) : null}
                  </td>
                  <td className="px-4 py-3">{r.debt_score ?? "—"}</td>
                  <td className="px-4 py-3">{r.agent_friction ?? "—"}</td>
                  <td className="px-4 py-3">{r.changeset_count}</td>
                  <td className="px-4 py-3 text-muted">
                    {r.mtime ? new Date(r.mtime * 1000).toLocaleString() : "—"}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <DeleteRunButton slug={r.slug} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {!runs.length && !error ? (
        <Card>
          <CardTitle>No runs yet</CardTitle>
          <CardDesc>Run analyze via MCP/agent (UI does not start analyze).</CardDesc>
        </Card>
      ) : null}
    </div>
  );
}
