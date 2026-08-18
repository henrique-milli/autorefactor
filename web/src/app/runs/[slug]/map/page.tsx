import Link from "next/link";
import { fetchChangesets, fetchGraph, fetchMetrics, fetchProjection } from "@/lib/api";
import { VectorMap } from "@/components/map/VectorMap";
import { Card, CardDesc, CardTitle } from "@/components/ui/card";

export const dynamic = "force-dynamic";

export default async function MapPage({
  params,
  searchParams,
}: {
  params: Promise<{ slug: string }>;
  searchParams: Promise<{ cs?: string }>;
}) {
  const { slug } = await params;
  const { cs } = await searchParams;
  try {
    const [proj, graph, changesets, metrics] = await Promise.all([
      fetchProjection(slug),
      fetchGraph(slug),
      fetchChangesets(slug).catch(() => []),
      fetchMetrics(slug).catch(() => null),
    ]);
    return (
      <div className="space-y-4">
        <div>
          <h1 className="text-4xl">Vector map</h1>
          <p className="mt-2 max-w-2xl text-muted">
            Units cluster by embedding similarity. Color = opportunity kind (or unit kind if
            not queued); larger glowing spheres are on the queue; red lines link near-duplicates.
            Hover for a name; click for why it was flagged.
          </p>
        </div>
        <VectorMap
          points={proj.points || []}
          edges={graph.edges || []}
          changesets={changesets}
          upsideById={Object.fromEntries(
            (metrics?.upside_by_changeset || []).map((row) => [row.id, row]),
          )}
          focusChangesetId={cs || null}
          slug={slug}
        />
      </div>
    );
  } catch (e) {
    const err = e as Error & { body?: { hint?: string } };
    return (
      <Card className="border-danger/40">
        <CardTitle>Projection missing</CardTitle>
        <CardDesc>
          {err.body?.hint ||
            err.message ||
            "Re-run analyze to build report/projection.json for this run."}
        </CardDesc>
        <Link href={`/runs/${slug}`} className="mt-3 inline-block text-accent hover:underline">
          Back to ROI
        </Link>
      </Card>
    );
  }
}
