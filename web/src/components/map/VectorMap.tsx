"use client";

import {
  Component,
  type ReactNode,
  useEffect,
  useMemo,
  useState,
} from "react";
import { Canvas } from "@react-three/fiber";
import { OrbitControls, Grid, Html, Line } from "@react-three/drei";
import type { ChangesetLite, GraphEdge, ProjectionPoint, UnitLite, UpsideRow } from "@/lib/api";
import { SnippetPreview } from "@/components/code/SnippetPreview";
import { Sheet } from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { KindBadge } from "@/components/ui/badge";
import Link from "next/link";

const KIND_COLOR: Record<string, string> = {
  function: "#5a7268",
  type: "#8a6a52",
  dedup: "#0f6e56",
  structure: "#c45c26",
  safety: "#2f6f4e",
  docs: "#3d6d8c",
  slop: "#b45309",
};

const COLOR_LEGEND: { key: string; label: string; color: string }[] = [
  { key: "dedup", label: "Dedup / share (queued)", color: KIND_COLOR.dedup },
  { key: "structure", label: "Structure / split (queued)", color: KIND_COLOR.structure },
  { key: "safety", label: "Tests / safety (queued)", color: KIND_COLOR.safety },
  { key: "docs", label: "Docs (queued)", color: KIND_COLOR.docs },
  { key: "slop", label: "Slop / draft (queued)", color: KIND_COLOR.slop },
  { key: "function", label: "Function (not queued)", color: KIND_COLOR.function },
  { key: "type", label: "Type (not queued)", color: KIND_COLOR.type },
];

function pointColor(p: ProjectionPoint): string {
  return (
    (p.upside_kind && KIND_COLOR[p.upside_kind]) ||
    KIND_COLOR[p.kind] ||
    "#5c564c"
  );
}

function pointRoleLabel(p: ProjectionPoint): string {
  if (p.upside_kind === "dedup") return "Dedup / share opportunity";
  if (p.upside_kind === "structure") return "Structure / split opportunity";
  if (p.upside_kind === "safety") return "Tests / safety opportunity";
  if (p.upside_kind === "docs") return "Docs opportunity";
  if (p.upside_kind === "slop") return "Slop / draft behavior";
  if (p.on_queue) return "Queued unit";
  return p.kind === "type" ? "Type (not on queue)" : "Function (not on queue)";
}

function detectWebGL(): boolean {
  if (typeof document === "undefined") return false;
  try {
    const canvas = document.createElement("canvas");
    const attrs = { failIfMajorPerformanceCaveat: false } as WebGLContextAttributes;
    const gl =
      canvas.getContext("webgl2", attrs) ||
      canvas.getContext("webgl", attrs) ||
      canvas.getContext("experimental-webgl", attrs);
    return !!gl;
  } catch {
    return false;
  }
}

function Points({
  points,
  selectedId,
  onSelect,
  onHover,
  focusIds,
}: {
  points: ProjectionPoint[];
  selectedId: string | null;
  onSelect: (p: ProjectionPoint) => void;
  onHover: (p: ProjectionPoint | null) => void;
  focusIds: Set<string> | null;
}) {
  return (
    <group>
      {points.map((p) => {
        const focusing = focusIds !== null;
        const focused = !focusing || focusIds.has(p.unit_id);
        const color = pointColor(p);
        const scale = p.on_queue ? 1.6 : 1;
        const opacity = focused ? 0.95 : 0.12;
        return (
          <mesh
            key={p.unit_id}
            position={[p.x, p.y, p.z]}
            scale={scale * (selectedId === p.unit_id ? 1.4 : 1)}
            onClick={(e) => {
              e.stopPropagation();
              onSelect(p);
            }}
            onPointerOver={(e) => {
              e.stopPropagation();
              document.body.style.cursor = "pointer";
              onHover(p);
            }}
            onPointerOut={() => {
              document.body.style.cursor = "auto";
              onHover(null);
            }}
          >
            <sphereGeometry args={[0.12, 16, 16]} />
            <meshStandardMaterial
              color={color}
              emissive={p.on_queue ? color : "#000000"}
              emissiveIntensity={p.on_queue ? 0.35 : 0}
              transparent
              opacity={opacity}
            />
          </mesh>
        );
      })}
    </group>
  );
}

function EdgeLines({
  edges,
  byId,
  focusCs,
}: {
  edges: GraphEdge[];
  byId: Map<string, ProjectionPoint>;
  focusCs: string | null;
}) {
  const segments = useMemo(() => {
    const out: { points: [number, number, number][]; color: string }[] = [];
    for (const e of edges) {
      const a = byId.get(e.source);
      const b = byId.get(e.target);
      if (!a || !b) continue;
      if (focusCs && e.changeset_id && e.changeset_id !== focusCs) continue;
      out.push({
        points: [
          [a.x, a.y, a.z],
          [b.x, b.y, b.z],
        ],
        color:
          e.kind === "clone" ? "#a33b2b" : e.kind === "depends_on" ? "#888888" : "#3d6d8c",
      });
    }
    return out;
  }, [edges, byId, focusCs]);

  return (
    <group>
      {segments.map((s, i) => (
        <Line key={i} points={s.points} color={s.color} lineWidth={1.5} transparent opacity={0.75} />
      ))}
    </group>
  );
}

function Scatter2D({
  points,
  edges,
  focusCs,
  focusIds,
  selectedId,
  onSelect,
  onHover,
}: {
  points: ProjectionPoint[];
  edges: GraphEdge[];
  focusCs: string | null;
  focusIds: Set<string> | null;
  selectedId: string | null;
  onSelect: (p: ProjectionPoint) => void;
  onHover: (p: ProjectionPoint | null) => void;
}) {
  const byId = useMemo(() => new Map(points.map((p) => [p.unit_id, p])), [points]);
  const pad = 24;
  const w = 900;
  const h = 520;

  const { sx, sy } = useMemo(() => {
    if (!points.length) {
      return {
        sx: () => w / 2,
        sy: () => h / 2,
      };
    }
    const xs = points.map((p) => p.x);
    const ys = points.map((p) => p.y);
    const minX = Math.min(...xs);
    const maxX = Math.max(...xs);
    const minY = Math.min(...ys);
    const maxY = Math.max(...ys);
    const dx = maxX - minX || 1;
    const dy = maxY - minY || 1;
    return {
      sx: (x: number) => pad + ((x - minX) / dx) * (w - pad * 2),
      sy: (y: number) => pad + ((maxY - y) / dy) * (h - pad * 2),
    };
  }, [points]);

  return (
    <div className="relative h-full w-full overflow-auto">
      <div className="absolute left-3 top-14 z-10 max-w-md rounded-lg border border-amber-500/40 bg-black/70 px-3 py-2 text-[11px] text-amber-100/90 backdrop-blur">
        WebGL is disabled in this browser, so the map is shown as a 2D PCA scatter (X/Y).
        Enable hardware acceleration / check{" "}
        <code className="text-amber-50">chrome://gpu</code> for a 3D view.
      </div>
      <svg viewBox={`0 0 ${w} ${h}`} className="h-full min-h-[420px] w-full" role="img">
        <rect width={w} height={h} fill="#15130f" />
        {edges.map((e, i) => {
          const a = byId.get(e.source);
          const b = byId.get(e.target);
          if (!a || !b) return null;
          if (focusCs && e.changeset_id && e.changeset_id !== focusCs) return null;
          const stroke =
            e.kind === "clone" ? "#a33b2b" : e.kind === "depends_on" ? "#888888" : "#3d6d8c";
          return (
            <line
              key={`${e.source}-${e.target}-${i}`}
              x1={sx(a.x)}
              y1={sy(a.y)}
              x2={sx(b.x)}
              y2={sy(b.y)}
              stroke={stroke}
              strokeWidth={1.2}
              opacity={0.7}
            />
          );
        })}
        {points.map((p) => {
          const focusing = focusIds !== null;
          const focused = !focusing || focusIds.has(p.unit_id);
          const r = (p.on_queue ? 5.5 : 3.5) * (selectedId === p.unit_id ? 1.35 : 1);
          return (
            <circle
              key={p.unit_id}
              cx={sx(p.x)}
              cy={sy(p.y)}
              r={r}
              fill={pointColor(p)}
              opacity={focused ? 0.95 : 0.15}
              className="cursor-pointer"
              onClick={() => onSelect(p)}
              onMouseEnter={() => onHover(p)}
              onMouseLeave={() => onHover(null)}
            />
          );
        })}
      </svg>
      <div className="pointer-events-none absolute bottom-3 left-3 text-[11px] text-white/60">
        2D fallback · hover for name · click a unit
      </div>
    </div>
  );
}

class WebGLErrorBoundary extends Component<
  { children: ReactNode; onError: () => void },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch() {
    this.props.onError();
  }

  render() {
    if (this.state.failed) return null;
    return this.props.children;
  }
}

const EDGE_LABEL: Record<string, string> = {
  clone: "Near-duplicate",
  cluster: "Same cluster",
  depends_on: "Depends on",
};

const EDGE_DOT: Record<string, string> = {
  clone: "#a33b2b",
  cluster: "#3d6d8c",
  depends_on: "#888888",
};

function shortName(qualified: string): string {
  const parts = qualified.split(".");
  return parts[parts.length - 1] || qualified;
}

function UnitDetail({
  selected,
  pointsById,
  unitsById,
  edges,
  changesetsById,
  upsideById,
  slug,
  onSelect,
  onFocusCs,
}: {
  selected: ProjectionPoint;
  pointsById: Map<string, ProjectionPoint>;
  unitsById: Map<string, UnitLite>;
  edges: GraphEdge[];
  changesetsById: Map<string, ChangesetLite>;
  upsideById: Record<string, UpsideRow>;
  slug: string;
  onSelect: (p: ProjectionPoint) => void;
  onFocusCs: (id: string) => void;
}) {
  const flags = (selected.changeset_ids || []).map((id) => {
    const cs = changesetsById.get(id);
    const up = upsideById[id];
    return { id, cs, up };
  });

  const neighbors = (() => {
    const seen = new Set<string>();
    const rows: {
      other: ProjectionPoint;
      kind: string;
      action: string;
      changesetId: string;
    }[] = [];
    for (const e of edges) {
      const otherId = e.source === selected.unit_id ? e.target : e.target === selected.unit_id ? e.source : null;
      if (!otherId) continue;
      const other = pointsById.get(otherId);
      if (!other) continue;
      const key = `${otherId}:${e.kind}:${e.changeset_id}`;
      if (seen.has(key)) continue;
      seen.add(key);
      rows.push({ other, kind: e.kind, action: e.action, changesetId: e.changeset_id });
    }
    rows.sort((a, b) => {
      const rank = (k: string) => (k === "clone" ? 0 : k === "cluster" ? 1 : 2);
      return rank(a.kind) - rank(b.kind) || a.other.qualified_name.localeCompare(b.other.qualified_name);
    });
    return rows;
  })();

  const color = pointColor(selected);
  const selectedUnit = unitsById.get(selected.unit_id);
  const cloneNeighbors = neighbors.filter((n) => n.kind === "clone");
  const codePairUnitIds = new Set<string>([selected.unit_id]);
  for (const n of cloneNeighbors.slice(0, 3)) {
    codePairUnitIds.add(n.other.unit_id);
  }

  return (
    <div className="space-y-5">
      <div className="flex items-start gap-3">
        <span
          className="mt-1 inline-block h-3.5 w-3.5 shrink-0 rounded-full ring-2 ring-black/10"
          style={{ background: color }}
          aria-hidden
        />
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <KindBadge kind={selected.upside_kind || selected.kind} />
            {selected.on_queue ? (
              <span className="rounded-md border border-queue/40 bg-orange-50 px-2 py-0.5 text-[11px] font-medium uppercase tracking-wide text-queue">
                On queue
              </span>
            ) : (
              <span className="rounded-md border border-line px-2 py-0.5 text-[11px] uppercase tracking-wide text-muted">
                Not queued
              </span>
            )}
          </div>
          <p className="mt-2 break-all font-mono text-[11px] text-muted">
            {selected.repo} / {selected.path}
          </p>
        </div>
      </div>

      <section>
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">
          {cloneNeighbors.length ? "Code pair" : "Code"}
        </h3>
        {cloneNeighbors.length ? (
          <div className="mt-2 grid gap-2 sm:grid-cols-2">
            <SnippetPreview
              path={`${selected.repo}/${selected.path}`}
              symbol={selected.qualified_name}
              snippet={selectedUnit?.source_snippet}
            />
            {cloneNeighbors.slice(0, 3).map((n) => {
              const otherUnit = unitsById.get(n.other.unit_id);
              return (
                <SnippetPreview
                  key={n.other.unit_id}
                  path={`${n.other.repo}/${n.other.path}`}
                  symbol={n.other.qualified_name}
                  snippet={otherUnit?.source_snippet}
                />
              );
            })}
          </div>
        ) : (
          <div className="mt-2">
            <SnippetPreview
              path={`${selected.repo}/${selected.path}`}
              symbol={selected.qualified_name}
              snippet={selectedUnit?.source_snippet}
            />
          </div>
        )}
      </section>

      <section>
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">Flagged for</h3>
        {flags.length ? (
          <ul className="mt-2 space-y-3">
            {flags.map(({ id, cs, up }) => (
              <li key={id} className="rounded-lg border border-line bg-paper/70 p-3">
                <div className="flex flex-wrap items-center gap-1.5">
                  <KindBadge kind={up?.upside_kind || selected.upside_kind || undefined} />
                  <code className="text-[11px] text-muted">{cs?.action || up?.action}</code>
                  {typeof (cs?.confidence ?? up?.confidence) === "number" ? (
                    <span className="text-[11px] text-muted">
                      conf {(cs?.confidence ?? up?.confidence ?? 0).toFixed(2)}
                    </span>
                  ) : null}
                  {cs?.status ? (
                    <span className="text-[11px] uppercase tracking-wide text-muted">{cs.status}</span>
                  ) : null}
                </div>
                <p className="mt-1.5 text-sm font-medium leading-snug">{cs?.title || up?.title || id}</p>
                {(cs?.why || cs?.rationale) ? (
                  <p className="mt-1 text-xs leading-relaxed text-ink">
                    <span className="font-medium text-muted">Why flagged: </span>
                    {cs.why || cs.rationale}
                  </p>
                ) : null}
                <p className="mt-1 text-xs leading-relaxed text-muted">
                  {cs?.benefit ||
                    up?.plain_english ||
                    up?.upside ||
                    cs?.brief ||
                    "Queued for this unit."}
                </p>
                {cs?.targets?.length ? (
                  <div className="mt-2">
                    {cs.compare_targets && cs.targets.length >= 2 ? (
                      <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wide text-muted">
                        Compared units
                      </p>
                    ) : cs.targets.length === 1 ? (
                      <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wide text-muted">
                        Flagged unit
                      </p>
                    ) : null}
                    <div
                      className={`grid gap-2 ${
                        cs.compare_targets && cs.targets.length >= 2
                          ? "sm:grid-cols-2"
                          : ""
                      }`}
                    >
                      {cs.targets.map((t, i) => {
                        if (t.unit_id && codePairUnitIds.has(t.unit_id)) return null;
                        return (
                          <SnippetPreview
                            key={`${id}-${t.unit_id || t.path || i}`}
                            path={
                              t.repo && t.path ? `${t.repo}/${t.path}` : t.path
                            }
                            symbol={t.symbol}
                            snippet={t.source_snippet}
                          />
                        );
                      })}
                    </div>
                  </div>
                ) : null}
                <div className="mt-2 flex flex-wrap gap-3 text-xs">
                  <Link className="text-accent hover:underline" href={`/runs/${slug}/queue`}>
                    Open in queue
                  </Link>
                  <button
                    type="button"
                    className="text-accent hover:underline"
                    onClick={() => onFocusCs(id)}
                  >
                    Focus on map
                  </button>
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-2 text-sm text-muted">
            This unit is on the map for embedding proximity. It is not a queue target.
          </p>
        )}
      </section>

      <section>
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">
          Related units
          {neighbors.length ? ` · ${neighbors.length}` : ""}
        </h3>
        {neighbors.length ? (
          <ul className="mt-2 space-y-2">
            {neighbors.map((n) => (
              <li key={`${n.other.unit_id}-${n.kind}-${n.changesetId}`}>
                <button
                  type="button"
                  className="w-full rounded-lg border border-line bg-card px-3 py-2 text-left hover:border-accent/40 hover:bg-accent-soft/40"
                  onClick={() => onSelect(n.other)}
                >
                  <div className="flex items-center gap-2">
                    <span
                      className="h-2.5 w-2.5 shrink-0 rounded-full"
                      style={{ background: EDGE_DOT[n.kind] || "#5c564c" }}
                    />
                    <span className="truncate text-sm font-medium">{shortName(n.other.qualified_name)}</span>
                    <span className="ml-auto shrink-0 text-[11px] text-muted">
                      {EDGE_LABEL[n.kind] || n.kind}
                    </span>
                  </div>
                  <p className="mt-0.5 truncate pl-4 font-mono text-[11px] text-muted">
                    {n.other.repo} / {n.other.path}
                  </p>
                </button>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-2 text-sm text-muted">
            No clone, cluster, or dependency edges from this unit.
          </p>
        )}
      </section>
    </div>
  );
}

function MapLegend() {
  return (
    <div className="absolute bottom-3 right-3 z-10 max-w-[15rem] rounded-lg border border-white/10 bg-black/55 px-3 py-2 text-[11px] text-white/85 backdrop-blur">
      <div className="mb-1.5 font-medium text-white">Legend</div>
      <ul className="space-y-1">
        {COLOR_LEGEND.map((row) => (
          <li key={row.key} className="flex items-center gap-2">
            <span
              className="h-2.5 w-2.5 shrink-0 rounded-full"
              style={{ background: row.color }}
            />
            <span>{row.label}</span>
          </li>
        ))}
      </ul>
      <div className="mt-2 space-y-1 border-t border-white/10 pt-2 text-white/70">
        <div className="flex items-center gap-2">
          <span className="inline-block h-3 w-3 rounded-full bg-white/80 shadow-[0_0_6px_rgba(255,255,255,0.7)]" />
          Larger + glow = on queue
        </div>
        <div className="flex items-center gap-2">
          <span className="inline-block h-0.5 w-4 bg-[#a33b2b]" />
          Red line = near-duplicate pair
        </div>
        <div className="flex items-center gap-2">
          <span className="inline-block h-0.5 w-4 bg-[#3d6d8c]" />
          Blue line = same changeset cluster
        </div>
      </div>
    </div>
  );
}

function HoverTip({ point }: { point: ProjectionPoint | null }) {
  if (!point) return null;
  return (
    <div className="pointer-events-none absolute left-3 top-14 z-20 max-w-sm rounded-lg border border-white/15 bg-black/75 px-3 py-2 text-xs text-white shadow-lg backdrop-blur">
      <div className="flex items-center gap-2">
        <span
          className="h-2.5 w-2.5 shrink-0 rounded-full"
          style={{ background: pointColor(point) }}
        />
        <span className="font-medium leading-snug">{shortName(point.qualified_name)}</span>
      </div>
      <div className="mt-1 text-[11px] text-white/70">{pointRoleLabel(point)}</div>
      <div className="mt-0.5 truncate font-mono text-[10px] text-white/50">
        {point.repo} / {point.path}
      </div>
    </div>
  );
}

function MapFilters({
  repo,
  setRepo,
  repos,
  queueOnly,
  setQueueOnly,
  q,
  setQ,
  focusCs,
  setFocusCs,
  focusIds,
}: {
  repo: string;
  setRepo: (v: string) => void;
  repos: string[];
  queueOnly: boolean;
  setQueueOnly: (v: boolean) => void;
  q: string;
  setQ: (v: string) => void;
  focusCs: string | null;
  setFocusCs: (v: string | null) => void;
  focusIds: Set<string> | null;
}) {
  return (
    <div className="absolute left-3 top-3 z-10 flex flex-wrap gap-2 rounded-lg border border-white/10 bg-black/50 p-2 text-xs text-white backdrop-blur">
      <select
        className="rounded border border-white/20 bg-black/40 px-2 py-1"
        value={repo}
        onChange={(e) => setRepo(e.target.value)}
      >
        <option value="all">All repos</option>
        {repos.map((r) => (
          <option key={r} value={r}>
            {r}
          </option>
        ))}
      </select>
      <label className="flex items-center gap-1 px-1">
        <input
          type="checkbox"
          checked={queueOnly}
          onChange={(e) => setQueueOnly(e.target.checked)}
        />
        Queue only
      </label>
      <input
        className="w-36 rounded border border-white/20 bg-black/40 px-2 py-1"
        placeholder="Search name"
        value={q}
        onChange={(e) => setQ(e.target.value)}
      />
      {focusCs ? (
        <Button size="sm" variant="outline" onClick={() => setFocusCs(null)}>
          Clear focus
        </Button>
      ) : null}
      {focusCs && focusIds && focusIds.size === 0 ? (
        <span className="rounded bg-danger/80 px-2 py-1 text-white">
          No units match focus {focusCs}
        </span>
      ) : null}
    </div>
  );
}

export function VectorMap({
  points,
  edges,
  changesets = [],
  units = [],
  upsideById = {},
  focusChangesetId,
  slug,
}: {
  points: ProjectionPoint[];
  edges: GraphEdge[];
  changesets?: ChangesetLite[];
  units?: UnitLite[];
  upsideById?: Record<string, UpsideRow>;
  focusChangesetId?: string | null;
  slug: string;
}) {
  const [repo, setRepo] = useState<string>("all");
  const [queueOnly, setQueueOnly] = useState(false);
  const [q, setQ] = useState("");
  const [selected, setSelected] = useState<ProjectionPoint | null>(null);
  const [hovered, setHovered] = useState<ProjectionPoint | null>(null);
  const [focusCs, setFocusCs] = useState<string | null>(focusChangesetId || null);
  const [webglOk, setWebglOk] = useState<boolean | null>(null);

  useEffect(() => {
    setFocusCs(focusChangesetId || null);
  }, [focusChangesetId]);

  useEffect(() => {
    setWebglOk(detectWebGL());
  }, []);

  const repos = useMemo(
    () => Array.from(new Set(points.map((p) => p.repo))).sort(),
    [points],
  );

  const filtered = useMemo(() => {
    return points.filter((p) => {
      if (repo !== "all" && p.repo !== repo) return false;
      if (queueOnly && !p.on_queue) return false;
      if (q && !p.qualified_name.toLowerCase().includes(q.toLowerCase())) return false;
      return true;
    });
  }, [points, repo, queueOnly, q]);

  const focusIds = useMemo(() => {
    if (!focusCs) return null;
    const ids = new Set<string>();
    for (const e of edges) {
      if (e.changeset_id === focusCs) {
        ids.add(e.source);
        ids.add(e.target);
      }
    }
    for (const p of points) {
      if (p.changeset_ids?.includes(focusCs)) ids.add(p.unit_id);
    }
    return ids;
  }, [focusCs, edges, points]);

  const scaled = useMemo(() => {
    if (!filtered.length) return filtered;
    const maxAbs = Math.max(
      ...filtered.flatMap((p) => [Math.abs(p.x), Math.abs(p.y), Math.abs(p.z)]),
      1e-6,
    );
    const s = 8 / maxAbs;
    return filtered.map((p) => ({
      ...p,
      x: p.x * s,
      y: p.y * s,
      z: p.z * s,
    }));
  }, [filtered]);

  const scaledById = useMemo(
    () => new Map(scaled.map((p) => [p.unit_id, p])),
    [scaled],
  );

  const pointsById = useMemo(
    () => new Map(points.map((p) => [p.unit_id, p])),
    [points],
  );

  const changesetsById = useMemo(
    () => new Map(changesets.map((c) => [c.id, c])),
    [changesets],
  );

  const unitsById = useMemo(
    () => new Map(units.map((u) => [u.id, u])),
    [units],
  );

  const selectedHasClone =
    !!selected &&
    edges.some(
      (e) =>
        e.kind === "clone" &&
        (e.source === selected.unit_id || e.target === selected.unit_id),
    );

  const use3d = webglOk === true;

  return (
    <div className="relative h-[min(78vh,720px)] w-full overflow-hidden rounded-xl border border-line bg-[#15130f]">
      <MapFilters
        repo={repo}
        setRepo={setRepo}
        repos={repos}
        queueOnly={queueOnly}
        setQueueOnly={setQueueOnly}
        q={q}
        setQ={setQ}
        focusCs={focusCs}
        setFocusCs={setFocusCs}
        focusIds={focusIds}
      />
      <MapLegend />
      <HoverTip point={hovered && hovered.unit_id !== selected?.unit_id ? hovered : null} />
      {webglOk === null ? (
        <div className="flex h-full items-center justify-center text-sm text-white/50">
          Checking WebGL…
        </div>
      ) : use3d ? (
        <WebGLErrorBoundary onError={() => setWebglOk(false)}>
          <Canvas
            camera={{ position: [10, 8, 12], fov: 50 }}
            gl={{
              antialias: true,
              failIfMajorPerformanceCaveat: false,
              powerPreference: "high-performance",
            }}
            onCreated={({ gl }) => {
              const el = gl.domElement;
              el.addEventListener(
                "webglcontextlost",
                (e) => {
                  e.preventDefault();
                  setWebglOk(false);
                },
                false,
              );
            }}
          >
            <color attach="background" args={["#15130f"]} />
            <fog attach="fog" args={["#15130f", 18, 42]} />
            <ambientLight intensity={0.55} />
            <directionalLight position={[8, 12, 4]} intensity={1.1} />
            <Grid
              infiniteGrid
              fadeDistance={40}
              sectionColor="#2a2620"
              cellColor="#1f1c18"
              position={[0, -6, 0]}
            />
            <EdgeLines edges={edges} byId={scaledById} focusCs={focusCs} />
            <Points
              points={scaled}
              selectedId={selected?.unit_id || null}
              onSelect={setSelected}
              onHover={setHovered}
              focusIds={focusIds}
            />
            <OrbitControls makeDefault enableDamping />
            <Html fullscreen style={{ pointerEvents: "none" }}>
              <div className="pointer-events-none absolute bottom-3 left-3 text-[11px] text-white/60">
                Drag to orbit · scroll to zoom · hover for name · click for details
              </div>
            </Html>
          </Canvas>
        </WebGLErrorBoundary>
      ) : (
        <Scatter2D
          points={scaled}
          edges={edges}
          focusCs={focusCs}
          focusIds={focusIds}
          selectedId={selected?.unit_id || null}
          onSelect={setSelected}
          onHover={setHovered}
        />
      )}
      <Sheet
        open={!!selected}
        onOpenChange={(o) => !o && setSelected(null)}
        title={selected ? shortName(selected.qualified_name) : "Unit"}
        wide={selectedHasClone}
      >
        {selected ? (
          <UnitDetail
            selected={selected}
            pointsById={pointsById}
            unitsById={unitsById}
            edges={edges}
            changesetsById={changesetsById}
            upsideById={upsideById}
            slug={slug}
            onSelect={setSelected}
            onFocusCs={(id) => {
              setFocusCs(id);
              setSelected(null);
            }}
          />
        ) : null}
      </Sheet>
    </div>
  );
}
