import Link from "next/link";

export default async function RunLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;
  return (
    <>
      <div className="-mt-4 mb-6 border-b border-line pb-3">
        <div className="flex flex-wrap items-center gap-3 text-sm">
          <span className="font-mono text-xs text-muted">{slug}</span>
          <Link href={`/runs/${slug}`} className="text-accent hover:underline">
            ROI
          </Link>
          <Link href={`/runs/${slug}/map`} className="text-accent hover:underline">
            Map
          </Link>
          <Link href={`/runs/${slug}/queue`} className="text-accent hover:underline">
            Queue
          </Link>
          <Link href={`/runs/${slug}/jobs`} className="text-accent hover:underline">
            Jobs
          </Link>
        </div>
      </div>
      {children}
    </>
  );
}
