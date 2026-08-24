import { cn } from "@/lib/utils";

export function SnippetPreview({
  path,
  symbol,
  snippet,
  className,
}: {
  path?: string;
  symbol?: string;
  snippet?: string | null;
  className?: string;
}) {
  const label = [path, symbol].filter(Boolean).join(" :: ");
  if (!snippet) {
    return (
      <p className={cn("text-xs text-muted", className)}>
        No source snippet for this unit.
      </p>
    );
  }
  return (
    <figure
      className={cn(
        "overflow-hidden rounded-lg border border-line bg-paper",
        className,
      )}
    >
      {label ? (
        <figcaption className="truncate border-b border-line px-3 py-1.5 font-mono text-[11px] text-muted">
          {label}
        </figcaption>
      ) : null}
      <pre className="max-h-64 overflow-auto p-3 font-mono text-[11px] leading-relaxed text-ink">
        {snippet}
      </pre>
    </figure>
  );
}
