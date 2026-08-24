"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import { patchChangesetDecision } from "@/lib/api";
import { Button } from "@/components/ui/button";

export function ReviewActions({
  slug,
  changesetId,
  userDecision,
  compact = false,
}: {
  slug: string;
  changesetId: string;
  userDecision?: string | null;
  compact?: boolean;
}) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const approved = userDecision === "proceed";
  const skipped = userDecision === "skip";
  const blocked = userDecision === "blocked";

  async function decide(decision: "proceed" | "skip" | "blocked" | null) {
    setError(null);
    setBusy(true);
    try {
      await patchChangesetDecision(slug, changesetId, decision);
      startTransition(() => router.refresh());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed");
    } finally {
      setBusy(false);
    }
  }

  const disabled = busy || pending;

  return (
    <div className={compact ? "mt-2" : "mt-3"}>
      <div className="flex flex-wrap items-center gap-2">
        {approved ? (
          <>
            <span className="rounded-md border border-emerald-300 bg-emerald-50 px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-emerald-800">
              Approved
            </span>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={disabled}
              onClick={() => void decide(null)}
            >
              Undo
            </Button>
          </>
        ) : skipped || blocked ? (
          <>
            <span className="rounded-md border border-line bg-paper px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-muted">
              {skipped ? "Skipped" : "Blocked"}
            </span>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={disabled}
              onClick={() => void decide(null)}
            >
              Undo
            </Button>
          </>
        ) : (
          <>
            <Button
              type="button"
              size="sm"
              disabled={disabled}
              onClick={() => void decide("proceed")}
            >
              Approve
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={disabled}
              onClick={() => void decide("skip")}
            >
              Skip
            </Button>
          </>
        )}
      </div>
      {error ? <p className="mt-1 text-xs text-red-700">{error}</p> : null}
      {!approved && !skipped && !blocked && !compact ? (
        <p className="mt-1.5 text-[11px] text-muted">
          Approve stores your decision; tell the agent to execute when ready.
        </p>
      ) : null}
    </div>
  );
}
