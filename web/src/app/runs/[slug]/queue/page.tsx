import Link from "next/link";
import { fetchChangesets, fetchMetrics, type ChangesetLite } from "@/lib/api";
import { SnippetPreview } from "@/components/code/SnippetPreview";
import { ReviewActions } from "@/components/queue/ReviewActions";
import { KindBadge } from "@/components/ui/badge";
import { Card, CardDesc, CardTitle } from "@/components/ui/card";

export const dynamic = "force-dynamic";

const HITL_REASON_LABELS: Record<string, string> = {
  slop_sensitive: "Slop / draft behavior",
  security_sensitive: "Security-sensitive",
  self_merge: "Possible self-merge false positive",
  low_confidence: "Low confidence",
  threshold_band: "Near-threshold — needs judgment",
  behavior_sensitive_dedup: "Behavior-sensitive dedup",
  architectural_wiring: "Architectural wiring",
  wiring_blocked_structural: "Blocked on wiring decision",
  framework_sensitive_split: "Framework-sensitive split",
  placement_risk: "Placement risk",
  blast_radius: "High blast radius",
};

const FINDING_KIND_LABELS: Record<string, string> = {
  draft_in_memory_map: "Draft in-memory map",
  disabled_startup_validation: "Disabled startup validation",
  committed_secret: "Committed secret",
};

function targetPath(t: { repo?: string; path?: string }) {
  return t.repo && t.path ? `${t.repo}/${t.path}` : t.path;
}

function reviewWhy(cs: ChangesetLite): string {
  return (
    cs.why ||
    cs.rationale ||
    (cs.brief && !cs.brief.startsWith("##") ? cs.brief : "") ||
    ""
  );
}

function reviewBenefit(cs: ChangesetLite, plainEnglish?: string): string {
  const benefit = cs.benefit || plainEnglish || "";
  const why = reviewWhy(cs);
  if (!benefit || benefit === why) return "";
  return benefit;
}

function TargetSnippets({
  targets,
  inlineCount = 2,
  collapsible = false,
  compare = false,
}: {
  targets: NonNullable<ChangesetLite["targets"]>;
  inlineCount?: number;
  collapsible?: boolean;
  compare?: boolean;
}) {
  if (!targets.length) return null;

  const showCount = compare ? Math.max(inlineCount, 2) : inlineCount;
  const inline = targets.slice(0, showCount || targets.length);
  const rest = showCount ? targets.slice(showCount) : [];

  const snippetBlock = (list: typeof targets, sideBySide: boolean) => (
    <div
      className={
        sideBySide && list.length >= 2
          ? "grid gap-2 sm:grid-cols-2"
          : "grid gap-2"
      }
    >
      {list.map((t, i) => (
        <div key={`${t.unit_id || t.path || i}`} className="min-w-0">
          {sideBySide && list.length >= 2 ? (
            <p className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-muted">
              {i === 0 ? "Copy A" : i === 1 ? "Copy B" : `Copy ${i + 1}`}
            </p>
          ) : null}
          <SnippetPreview
            path={targetPath(t)}
            symbol={t.symbol}
            snippet={t.source_snippet}
          />
        </div>
      ))}
    </div>
  );

  if (!collapsible) {
    return (
      <div className="mt-2">
        {compare && targets.length >= 2 ? (
          <p className="mb-1.5 text-[11px] font-medium text-muted">
            Compared units — same logic, different places
          </p>
        ) : targets.length === 1 ? (
          <p className="mb-1.5 text-[11px] font-medium text-muted">
            Flagged unit (not a clone pair)
          </p>
        ) : null}
        {snippetBlock(inline, compare)}
        {rest.length ? (
          <details className="mt-2">
            <summary className="cursor-pointer text-xs text-accent hover:underline">
              Show all {targets.length} targets
            </summary>
            <div className="mt-2">{snippetBlock(rest, false)}</div>
          </details>
        ) : null}
      </div>
    );
  }

  return (
    <details className="mt-2">
      <summary className="cursor-pointer text-xs text-accent hover:underline">
        Show code ({targets.length} {targets.length === 1 ? "target" : "targets"})
      </summary>
      <div className="mt-2">{snippetBlock(targets, compare)}</div>
    </details>
  );
}

function ReviewCopy({
  cs,
  plainEnglish,
}: {
  cs: ChangesetLite;
  plainEnglish?: string;
}) {
  const why = reviewWhy(cs);
  const benefit = reviewBenefit(cs, plainEnglish);
  const reasonLabel =
    (cs.finding_kind && FINDING_KIND_LABELS[cs.finding_kind]) ||
    (cs.hitl_reason && HITL_REASON_LABELS[cs.hitl_reason]) ||
    cs.hitl_reason ||
    null;

  return (
    <>
      <div className="flex flex-wrap items-center gap-2">
        {reasonLabel ? (
          <span className="rounded-md border border-amber-400/50 bg-amber-100 px-2 py-0.5 text-[11px] font-medium text-amber-950 dark:bg-amber-900/40 dark:text-amber-100">
            {reasonLabel}
          </span>
        ) : null}
        {cs.upside_kind ? <KindBadge kind={cs.upside_kind} /> : null}
        <code className="text-[11px] text-muted">{cs.action}</code>
      </div>
      {why ? (
        <p className="mt-2 text-sm leading-relaxed text-ink">
          <span className="font-medium text-muted">Why flagged: </span>
          {why}
        </p>
      ) : (
        <p className="mt-2 text-sm text-muted">No model rationale for this item.</p>
      )}
      {benefit ? (
        <p className="mt-1 text-xs leading-relaxed text-muted">
          <span className="font-medium">Expected benefit: </span>
          {benefit}
        </p>
      ) : null}
    </>
  );
}

export default async function QueuePage({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;
  let rows: ChangesetLite[] = [];
  const valueById: Record<
    string,
    { value_score?: number; upside_kind?: string; plain_english?: string }
  > = {};
  try {
    const [changesets, metrics] = await Promise.all([
      fetchChangesets(slug),
      fetchMetrics(slug),
    ]);
    rows = changesets;
    for (const u of metrics.upside_by_changeset || []) {
      valueById[u.id] = u;
    }
    rows = [...rows].sort((a, b) => {
      const va = valueById[a.id]?.value_score ?? a.confidence ?? 0;
      const vb = valueById[b.id]?.value_score ?? b.confidence ?? 0;
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

  const reviewNext = rows.find(
    (cs) =>
      cs.needs_review &&
      (cs.status === "pending" || cs.status === "in_progress" || !cs.status),
  );
  const approvedCount = rows.filter(
    (cs) =>
      cs.user_decision === "proceed" &&
      (cs.status === "pending" || cs.status === "in_progress"),
  ).length;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-4xl">Queue</h1>
        <p className="mt-2 text-muted">
          Sorted by narrative value score when available. Approve or Skip review
          items here, then tell the agent to execute.
        </p>
        {approvedCount ? (
          <p className="mt-1 text-sm text-emerald-800">
            {approvedCount} approved — ask the agent to execute the approved queue.
          </p>
        ) : null}
      </div>

      {reviewNext ? (
        <section className="rounded-xl border border-amber-300/60 bg-amber-50/50 p-4 dark:border-amber-700/50 dark:bg-amber-950/20">
          <div className="flex flex-wrap items-center gap-2">
            <span className="rounded-md border border-amber-400/60 bg-amber-100 px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-amber-900 dark:bg-amber-900/40 dark:text-amber-100">
              Review next
            </span>
          </div>
          <p className="mt-2 font-medium">{reviewNext.title}</p>
          <div className="mt-2">
            <ReviewCopy
              cs={reviewNext}
              plainEnglish={valueById[reviewNext.id]?.plain_english}
            />
          </div>
          <TargetSnippets
            targets={reviewNext.targets || []}
            inlineCount={reviewNext.compare_targets ? 2 : 1}
            compare={Boolean(reviewNext.compare_targets)}
          />
          <ReviewActions
            slug={slug}
            changesetId={reviewNext.id}
            userDecision={reviewNext.user_decision}
          />
          <div className="mt-3 flex flex-wrap gap-3 text-sm">
            <Link
              href={`/runs/${slug}/map?cs=${encodeURIComponent(reviewNext.id)}`}
              className="text-accent hover:underline"
            >
              Focus on map
            </Link>
          </div>
        </section>
      ) : null}

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
              const targets = cs.targets || [];
              const isReview =
                cs.needs_review &&
                (cs.status === "pending" ||
                  cs.status === "in_progress" ||
                  !cs.status);
              const isApproved =
                cs.user_decision === "proceed" &&
                (cs.status === "pending" || cs.status === "in_progress");
              const showActions =
                isReview ||
                Boolean(cs.user_decision) ||
                cs.status === "skipped" ||
                cs.status === "blocked";
              return (
                <tr
                  key={cs.id}
                  className={`border-t border-line/70 align-top ${
                    isReview
                      ? "bg-amber-50/40 dark:bg-amber-950/10"
                      : isApproved
                        ? "bg-emerald-50/40 dark:bg-emerald-950/10"
                        : ""
                  }`}
                >
                  <td className="px-4 py-3">
                    <div className="font-semibold">
                      {(meta.value_score ?? cs.confidence ?? 0).toFixed(2)}
                    </div>
                    <KindBadge kind={cs.upside_kind || meta.upside_kind} />
                  </td>
                  <td className="px-4 py-3 font-mono text-xs">{cs.action}</td>
                  <td className="px-4 py-3">{(cs.confidence ?? 0).toFixed(2)}</td>
                  <td className="px-4 py-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">{cs.title}</span>
                      {isReview ? (
                        <span className="rounded-md border border-amber-400/50 bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-900 dark:bg-amber-900/30 dark:text-amber-100">
                          Needs review
                        </span>
                      ) : null}
                      {isApproved ? (
                        <span className="rounded-md border border-emerald-300 bg-emerald-50 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-emerald-800">
                          Approved
                        </span>
                      ) : null}
                    </div>
                    {isReview ? (
                      <div className="mt-2">
                        <ReviewCopy cs={cs} plainEnglish={meta.plain_english} />
                      </div>
                    ) : (
                      <p className="mt-1 text-xs text-muted">
                        {reviewWhy(cs) ||
                          meta.plain_english ||
                          cs.brief ||
                          cs.rationale}
                      </p>
                    )}
                    {cs.status ? (
                      <p className="mt-1 text-[11px] uppercase tracking-wide text-muted">
                        {cs.status}
                      </p>
                    ) : null}
                    {targets.length ? (
                      <TargetSnippets
                        targets={targets}
                        inlineCount={isReview ? (cs.compare_targets ? 2 : 1) : 0}
                        collapsible={!isReview}
                        compare={Boolean(isReview && cs.compare_targets)}
                      />
                    ) : null}
                    {showActions ? (
                      <ReviewActions
                        slug={slug}
                        changesetId={cs.id}
                        userDecision={cs.user_decision}
                        compact
                      />
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
