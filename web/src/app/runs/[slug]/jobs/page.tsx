"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { cancelJob, fetchJobs } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardDesc, CardTitle } from "@/components/ui/card";

export default function JobsPage() {
  const params = useParams<{ slug: string }>();
  const slug = params?.slug || "";
  const [jobs, setJobs] = useState<Record<string, unknown>[]>([]);
  const [msg, setMsg] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const refresh = async () => {
    try {
      const data = await fetchJobs(30);
      setJobs(data.jobs || []);
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Failed to load jobs");
    }
  };

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 4000);
    return () => clearInterval(t);
  }, []);

  const onCancel = async (jobId: string) => {
    setBusyId(jobId);
    setMsg(null);
    try {
      const res = await cancelJob(jobId);
      setMsg(JSON.stringify(res));
      await refresh();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "cancel failed");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-4xl">Jobs</h1>
        <p className="mt-2 text-muted">
          Background analyze jobs (server-wide). Start analyze via MCP/agent — this page
          only views and cancels. Run context: {slug || "…"}
        </p>
      </div>
      {msg ? (
        <Card>
          <CardTitle>Last response</CardTitle>
          <CardDesc className="break-all font-mono text-xs">{msg}</CardDesc>
        </Card>
      ) : null}
      <div className="overflow-hidden rounded-xl border border-line bg-card">
        <table className="w-full text-sm">
          <thead className="bg-paper/80 text-left text-xs uppercase tracking-wide text-muted">
            <tr>
              <th className="px-4 py-3">ID</th>
              <th className="px-4 py-3">Status</th>
              <th className="px-4 py-3">Phase</th>
              <th className="px-4 py-3">%</th>
              <th className="px-4 py-3">Stale</th>
              <th className="px-4 py-3">Updated</th>
              <th className="px-4 py-3">Error</th>
              <th className="px-4 py-3" />
            </tr>
          </thead>
          <tbody>
            {jobs.map((j) => {
              const jobId = String(j.job_id || j.id || "");
              const status = String(j.status || "");
              const canCancel = status === "running";
              const stale =
                typeof j.stale_seconds === "number"
                  ? `${Math.round(j.stale_seconds as number)}s`
                  : "—";
              return (
                <tr key={jobId} className="border-t border-line/70">
                  <td className="px-4 py-3 font-mono text-xs">{jobId}</td>
                  <td className="px-4 py-3">{status}</td>
                  <td className="px-4 py-3">{String(j.phase || "—")}</td>
                  <td className="px-4 py-3">
                    {typeof j.percent === "number" ? `${j.percent}%` : "—"}
                  </td>
                  <td className="px-4 py-3 text-muted">{stale}</td>
                  <td className="px-4 py-3 text-muted">{String(j.updated_at || "—")}</td>
                  <td className="px-4 py-3 text-xs text-danger">
                    {j.error ? String(j.error) : "—"}
                  </td>
                  <td className="px-4 py-3 text-right">
                    {canCancel ? (
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={busyId === jobId}
                        onClick={() => onCancel(jobId)}
                      >
                        {busyId === jobId ? "Cancelling…" : "Cancel"}
                      </Button>
                    ) : null}
                  </td>
                </tr>
              );
            })}
            {!jobs.length ? (
              <tr>
                <td colSpan={8} className="px-4 py-8 text-center text-muted">
                  No jobs yet
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </div>
  );
}
