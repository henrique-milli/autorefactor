"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { deleteRun } from "@/lib/api";
import { Button } from "@/components/ui/button";

export function DeleteRunButton({
  slug,
  redirectTo,
}: {
  slug: string;
  redirectTo?: string;
}) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const onConfirmDelete = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const res = await deleteRun(slug);
      if (!res.ok) {
        setMsg(res.error || "Delete failed");
        return;
      }
      setOpen(false);
      if (redirectTo) {
        router.push(redirectTo);
        router.refresh();
      } else {
        router.refresh();
      }
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Delete failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="inline-flex flex-col items-start gap-1">
      <Button
        variant="danger"
        size="sm"
        onClick={() => {
          setMsg(null);
          setOpen(true);
        }}
        disabled={busy}
      >
        Delete run
      </Button>
      {msg && !open ? <span className="text-xs text-danger">{msg}</span> : null}

      <Dialog.Root
        open={open}
        onOpenChange={(next) => {
          if (!busy) setOpen(next);
        }}
      >
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-40 bg-ink/40" />
          <Dialog.Content className="fixed left-1/2 top-1/2 z-50 w-[min(100%-2rem,24rem)] -translate-x-1/2 -translate-y-1/2 rounded-xl border border-line bg-card p-5 shadow-xl focus:outline-none">
            <Dialog.Title className="font-display text-xl text-ink">
              Delete this run?
            </Dialog.Title>
            <Dialog.Description className="mt-2 text-sm text-muted">
              Removes report artifacts and related analyze jobs for{" "}
              <span className="font-mono text-xs text-ink">{slug}</span>. The synced
              workspace is kept.
            </Dialog.Description>
            {msg ? <p className="mt-3 text-xs text-danger">{msg}</p> : null}
            <div className="mt-5 flex justify-end gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={busy}
                onClick={() => setOpen(false)}
              >
                Cancel
              </Button>
              <Button
                variant="danger"
                size="sm"
                disabled={busy}
                onClick={onConfirmDelete}
              >
                {busy ? "Deleting…" : "Delete"}
              </Button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
  );
}
