import Link from "next/link";

const links = [
  { href: "/runs", label: "Runs" },
  { href: "/settings", label: "Settings" },
];

export function AppNav({ slug }: { slug?: string }) {
  return (
    <header className="border-b border-line/80 bg-card/70 backdrop-blur">
      <div className="mx-auto flex max-w-6xl items-center justify-between gap-6 px-6 py-4">
        <Link href="/runs" className="font-display text-2xl tracking-tight text-ink">
          autorefactor
        </Link>
        <nav className="flex flex-wrap items-center gap-4 text-sm">
          {links.map((l) => (
            <Link key={l.href} href={l.href} className="text-muted hover:text-accent">
              {l.label}
            </Link>
          ))}
          {slug ? (
            <>
              <Link href={`/runs/${slug}`} className="text-muted hover:text-accent">
                ROI
              </Link>
              <Link href={`/runs/${slug}/map`} className="text-muted hover:text-accent">
                Map
              </Link>
              <Link href={`/runs/${slug}/queue`} className="text-muted hover:text-accent">
                Queue
              </Link>
              <Link href={`/runs/${slug}/jobs`} className="text-muted hover:text-accent">
                Jobs
              </Link>
            </>
          ) : null}
        </nav>
      </div>
    </header>
  );
}
