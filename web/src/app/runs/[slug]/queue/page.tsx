import Link from "next/link";
import { fetchChangesets, fetchMetrics } from "@/lib/api";
import { KindBadge } from "@/components/ui/badge";
import { Card, CardDesc, CardTitle } from "@/components/ui/card";

export const dynamic = "force-dynamic";

type Cs = {
  id: string;
  title: string;
  action: string;
  confidence: number;
  brief?: string;
  rationale?: string;
  metrics_delta?: Record<string, unknown>;
  status?: string;
};

export default async function QueuePage({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;
  let rows: Cs[] = [];
  const valueById: Record<
    string,
    { value_score?: number; upside_kind?: string; plain_english?: string }
  > = {};
  try {
    const [changesets, metrics] = await Promise.all([
      fetchChangesets(slug),
      fetchMetrics(slug),
    ]);
    rows = changesets as Cs[];
    for (const u of metrics.upside_by_changeset || []) {
      valueById[u.id] = u;
    }
    rows = [...rows].sort((a, b) => {
      const va = valueById[a.id]?.value_score ?? a.confidence;
      const vb = valueById[b.id]?.value_score ?? b.confidence;
      return (vb || 0) - (va || 0);
    });
  } catch (e) {
    return (
      <Card>
        <CardTitle>Queue unavailable</CardTitle>
        <CardDesc>{e instanceof Error ? e.message : "error"}</CardDesc>
      </Card>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-4xl">Queue</h1>
        <p className="mt-2 text-muted">Sorted by narrative value score when available.</p>
      </div>
      <div className="overflow-hidden rounded-xl border border-line bg-card">
        <table className="w-full text-sm">
          <thead className="bg-paper/80 text-left text-xs uppercase tracking-wide text-muted">
            <tr>
              <th className="px-4 py-3">Value</th>
              <th className="px-4 py-3">Action</th>
              <th className="px-4 py-3">Conf</th>
              <th className="px-4 py-3">Title</th>
              <th className="px-4 py-3">Map</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((cs) => {
              const meta = valueById[cs.id] || {};
              return (
                <tr key={cs.id} className="border-t border-line/70 align-top">
                  <td className="px-4 py-3">
                    <div className="font-semibold">
                      {(meta.value_score ?? cs.confidence).toFixed(2)}
                    </div>
                    <KindBadge kind={meta.upside_kind} />
                  </td>
                  <td className="px-4 py-3 font-mono text-xs">{cs.action}</td>
                  <td className="px-4 py-3">{cs.confidence.toFixed(2)}</td>
                  <td className="px-4 py-3">
                    <div className="font-medium">{cs.title}</div>
                    <p className="mt-1 text-xs text-muted">
                      {meta.plain_english || cs.brief || cs.rationale}
                    </p>
                    {cs.status ? (
                      <p className="mt-1 text-[11px] uppercase tracking-wide text-muted">
                        {cs.status}
                      </p>
                    ) : null}
                  </td>
                  <td className="px-4 py-3">
                    <Link
                      href={`/runs/${slug}/map?cs=${encodeURIComponent(cs.id)}`}
                      className="text-accent hover:underline"
                    >
                      Focus
                    </Link>
                  </td>
                </tr>
              );
            })}
            {!rows.length ? (
              <tr>
                <td colSpan={5} className="px-4 py-8 text-center text-muted">
                  Empty queue
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </div>
  );
}
