import type { HTMLAttributes } from "react";
import { cn } from "@/lib/utils";

export function Badge({
  className,
  ...props
}: HTMLAttributes<HTMLSpanElement>) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-md border border-line bg-paper px-2 py-0.5 text-xs font-medium text-muted",
        className,
      )}
      {...props}
    />
  );
}

export function KindBadge({ kind }: { kind?: string }) {
  const colors: Record<string, string> = {
    dedup: "border-accent/40 bg-accent-soft text-accent",
    structure: "border-queue/40 bg-orange-50 text-queue",
    safety: "border-emerald-300 bg-emerald-50 text-emerald-800",
    docs: "border-sky-300 bg-sky-50 text-sky-800",
  };
  const k = kind || "structure";
  return <Badge className={colors[k] || ""}>{k}</Badge>;
}
