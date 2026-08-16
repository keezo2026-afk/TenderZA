"use client";

// Source-health dashboard (Blueprint §15, §16).
//
// This is the ops view the blueprint insists must "page someone": the
// alarm banner at the top mirrors exactly what scripts/check_source_health.py
// sends to the pager, so the screen and the pager can never disagree.

import { useCallback, useEffect, useState } from "react";

interface Verdict {
  source_id: string;
  name: string;
  state: string;
  tier: number | null;
  reason: string;
  minutes_since_success: number | null;
  sla_minutes: number;
  sla_breached: boolean;
  breakage_minutes: number | null;
  page: boolean;
  consecutive_failures: number;
  last_error: string | null;
  detail: Record<string, unknown>;
}

interface Overview {
  generated_at: string;
  summary: {
    sources_total: number;
    crawlable: number;
    healthy: number;
    healthy_pct: number | null;
    by_state: Record<string, number>;
    paging: number;
    sla_target_pct: number;
    sla_met: boolean;
  };
  crawl_24h: {
    jobs_total: number;
    jobs_by_state: Record<string, number>;
    job_success_pct: number | null;
    tenders_created: number;
    tenders_updated: number;
    crawl_results: number;
    documents_stored: number;
    review_queue_open: number;
  };
  freshness: Array<{
    triage_tier: number | null;
    sources: number;
    ever_succeeded: number;
    avg_age_min: number | null;
    max_age_min: number | null;
    sla_minutes: number;
    within_sla: boolean;
  }>;
  adapters: Array<{
    adapter: string;
    sources: number;
    runs: number;
    ok: number;
    failed: number;
    created: number;
    success_pct: number | null;
    silent: boolean;
  }>;
  failures: Array<{
    source_name: string;
    triage_tier: number | null;
    checked_at: string;
    last_error: string | null;
    status_code: number | null;
  }>;
  reliability: {
    episodes: number;
    resolved: number;
    open: number;
    avg_mttr_min: number | null;
    max_mttr_min: number | null;
    mttr_target_min: number;
    mttr_met: boolean;
  };
  sources: Verdict[];
}

const STATE_STYLE: Record<string, { dot: string; label: string; badge: string }> = {
  OK: { dot: "🟢", label: "OK", badge: "bg-emerald-50 text-emerald-700 border-emerald-200" },
  STALE: { dot: "🟡", label: "Stale", badge: "bg-amber-50 text-amber-700 border-amber-200" },
  DEGRADED: { dot: "🟠", label: "Degraded", badge: "bg-orange-50 text-orange-700 border-orange-200" },
  FAILED: { dot: "🔴", label: "Failed", badge: "bg-red-50 text-red-700 border-red-200" },
  NEVER_RUN: { dot: "⚫", label: "Never run", badge: "bg-slate-100 text-slate-600 border-slate-300" },
  DISCOVERY: { dot: "🔵", label: "Discovery", badge: "bg-sky-50 text-sky-700 border-sky-200" },
  PUBLISH_NOTHING: { dot: "⚪", label: "Publishes nothing", badge: "bg-slate-50 text-slate-500 border-slate-200" },
};

function age(min: number | null): string {
  if (min === null || min === undefined) return "never";
  if (min < 60) return `${Math.round(min)} min ago`;
  if (min < 1440) return `${(min / 60).toFixed(1)} h ago`;
  return `${(min / 1440).toFixed(1)} d ago`;
}

export default function OpsPage() {
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<string>("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/ops/overview");
      if (!res.ok) throw new Error(`load failed: ${res.status}`);
      setData(await res.json());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "load failed");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 30_000); // live ops view
    return () => clearInterval(t);
  }, [load]);

  if (loading) return <p className="text-sm text-slate-500">Loading source health…</p>;
  if (error)
    return (
      <p className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700">
        {error}
      </p>
    );
  if (!data) return null;

  const { summary, crawl_24h: crawl, reliability } = data;
  const alarms = data.sources.filter((s) => s.page);
  const shown = filter ? data.sources.filter((s) => s.state === filter) : data.sources;

  return (
    <div className="space-y-6">
      <div className="flex items-baseline justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Source health</h1>
          <p className="text-sm text-slate-500">
            Crawler operations · refreshes every 30s ·{" "}
            {new Date(data.generated_at).toLocaleTimeString("en-ZA", {
              timeZone: "Africa/Johannesburg",
            })}{" "}
            SAST
          </p>
        </div>
        <button
          onClick={load}
          className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm hover:bg-slate-50"
        >
          Refresh
        </button>
      </div>

      {/* Alarm banner — the "must page someone" surface (§15) */}
      {alarms.length > 0 ? (
        <section className="rounded-lg border border-red-300 bg-red-50 p-4">
          <h2 className="flex items-center gap-2 font-semibold text-red-800">
            <span>🚨</span> {alarms.length} source{alarms.length === 1 ? "" : "s"} need
            attention
          </h2>
          <ul className="mt-2 space-y-2">
            {alarms.map((a) => (
              <li key={a.source_id} className="text-sm text-red-900">
                <span className="font-medium">
                  {STATE_STYLE[a.state]?.dot} {a.name}
                </span>{" "}
                <span className="text-red-700">
                  (T{a.tier ?? "?"} · last success {age(a.minutes_since_success)})
                </span>
                <div className="text-xs text-red-700">{a.reason}</div>
              </li>
            ))}
          </ul>
          <p className="mt-3 text-xs text-red-700">
            The same list is sent by{" "}
            <code className="rounded bg-red-100 px-1">check_source_health.py --notify</code>{" "}
            — a dashboard nobody gets alerted on is decoration.
          </p>
        </section>
      ) : (
        <section className="rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800">
          ✅ No sources need attention — all crawlable sources within their tier SLA.
        </section>
      )}

      {/* Headline KPIs (§16) */}
      <section className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Kpi
          label="Healthy sources"
          value={
            summary.healthy_pct !== null ? `${summary.healthy_pct}%` : "—"
          }
          sub={`${summary.healthy}/${summary.crawlable} crawlable`}
          good={summary.sla_met}
          target={`target ≥${summary.sla_target_pct}%`}
        />
        <Kpi
          label="Job success (24h)"
          value={crawl.job_success_pct !== null ? `${crawl.job_success_pct}%` : "—"}
          sub={`${crawl.jobs_total} jobs run`}
          good={(crawl.job_success_pct ?? 0) >= 99}
          target="target ≥99%"
        />
        <Kpi
          label="New tenders (24h)"
          value={String(crawl.tenders_created)}
          sub={`${crawl.tenders_updated} updated`}
        />
        <Kpi
          label="Review queue"
          value={String(crawl.review_queue_open)}
          sub="items awaiting a human"
        />
      </section>

      {/* Freshness per tier (§16 SLA table) */}
      <section className="rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="mb-3 font-semibold">Freshness by tier</h2>
        <table className="w-full text-sm">
          <thead className="text-left text-xs uppercase tracking-wide text-slate-400">
            <tr>
              <th className="pb-2">Tier</th>
              <th className="pb-2">Sources</th>
              <th className="pb-2">SLA</th>
              <th className="pb-2">Avg age</th>
              <th className="pb-2">Worst age</th>
              <th className="pb-2"></th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {data.freshness.map((f) => (
              <tr key={String(f.triage_tier)}>
                <td className="py-2 font-medium">T{f.triage_tier ?? "?"}</td>
                <td className="py-2 text-slate-600">
                  {f.ever_succeeded}/{f.sources} crawled
                </td>
                <td className="py-2 text-slate-600">{f.sla_minutes} min</td>
                <td className="py-2 text-slate-600">{age(f.avg_age_min)}</td>
                <td className="py-2 text-slate-600">{age(f.max_age_min)}</td>
                <td className="py-2">
                  {f.max_age_min === null ? (
                    <span className="text-xs text-slate-400">no data</span>
                  ) : f.within_sla ? (
                    <span className="text-xs text-emerald-600">within SLA</span>
                  ) : (
                    <span className="text-xs text-amber-600">breaching</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {/* Adapter rot (§15, §19) */}
      <section className="rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="font-semibold">Adapter health (7 days)</h2>
        <p className="mb-3 text-xs text-slate-500">
          Site redesigns show up here first: runs that succeed but yield nothing are
          flagged <span className="font-medium text-amber-700">silent</span>.
        </p>
        <div className="space-y-2">
          {data.adapters.map((a) => (
            <div
              key={a.adapter}
              className="flex items-center justify-between rounded border border-slate-100 px-3 py-2 text-sm"
            >
              <div>
                <span className="font-mono text-xs">{a.adapter}</span>
                <span className="ml-2 text-slate-500">
                  {a.sources} source{a.sources === 1 ? "" : "s"} · {a.runs} run
                  {a.runs === 1 ? "" : "s"} · {a.created} new tenders
                </span>
              </div>
              <div className="flex items-center gap-2">
                {a.silent && (
                  <span className="rounded border border-amber-200 bg-amber-50 px-2 py-0.5 text-xs text-amber-700">
                    silent — possible rot
                  </span>
                )}
                <span
                  className={
                    a.success_pct === null
                      ? "text-xs text-slate-400"
                      : a.success_pct >= 99
                        ? "text-xs text-emerald-600"
                        : "text-xs text-amber-600"
                  }
                >
                  {a.success_pct === null ? "no runs" : `${a.success_pct}% ok`}
                </span>
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* Per-source table */}
      <section className="rounded-lg border border-slate-200 bg-white p-4">
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <h2 className="mr-2 font-semibold">Sources ({data.sources.length})</h2>
          <button
            onClick={() => setFilter("")}
            className={`rounded-full border px-2 py-0.5 text-xs ${
              filter === "" ? "border-slate-900 bg-slate-900 text-white" : "border-slate-300"
            }`}
          >
            all
          </button>
          {Object.entries(summary.by_state).map(([state, n]) => (
            <button
              key={state}
              onClick={() => setFilter(state === filter ? "" : state)}
              className={`rounded-full border px-2 py-0.5 text-xs ${
                filter === state
                  ? "border-slate-900 bg-slate-900 text-white"
                  : STATE_STYLE[state]?.badge ?? "border-slate-300"
              }`}
            >
              {STATE_STYLE[state]?.dot} {STATE_STYLE[state]?.label ?? state} {n}
            </button>
          ))}
        </div>
        <div className="divide-y divide-slate-100">
          {shown.map((s) => (
            <div key={s.source_id} className="py-2.5">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span>{STATE_STYLE[s.state]?.dot ?? "•"}</span>
                    <span className="truncate font-medium">{s.name}</span>
                    <span className="shrink-0 rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-600">
                      T{s.tier ?? "?"}
                    </span>
                    {s.page && (
                      <span className="shrink-0 rounded bg-red-100 px-1.5 py-0.5 text-[11px] font-medium text-red-700">
                        PAGING
                      </span>
                    )}
                  </div>
                  <p className="mt-0.5 text-xs text-slate-500">{s.reason}</p>
                  {s.last_error && (
                    <p className="mt-0.5 truncate font-mono text-[11px] text-red-600">
                      {s.last_error}
                    </p>
                  )}
                </div>
                <div className="shrink-0 text-right text-xs text-slate-500">
                  <div>last success {age(s.minutes_since_success)}</div>
                  <div className="text-[11px] text-slate-400">
                    SLA {s.sla_minutes} min
                    {s.consecutive_failures > 0 &&
                      ` · ${s.consecutive_failures} fail${s.consecutive_failures === 1 ? "" : "s"}`}
                  </div>
                </div>
              </div>
            </div>
          ))}
          {shown.length === 0 && (
            <p className="py-3 text-sm text-slate-500">No sources in this state.</p>
          )}
        </div>
      </section>

      {/* Reliability (§16 MTTD/MTTR) */}
      <section className="rounded-lg border border-slate-200 bg-white p-4 text-sm">
        <h2 className="mb-2 font-semibold">Reliability (30 days)</h2>
        <p className="text-slate-600">
          {reliability.episodes} breakage episode
          {reliability.episodes === 1 ? "" : "s"} · {reliability.open} still open
          {reliability.avg_mttr_min !== null && (
            <>
              {" "}
              · avg MTTR {age(reliability.avg_mttr_min)}
              <span className={reliability.mttr_met ? "text-emerald-600" : "text-amber-600"}>
                {" "}
                (target &lt; 24 h)
              </span>
            </>
          )}
        </p>
        {data.failures.length > 0 && (
          <details className="mt-3">
            <summary className="cursor-pointer text-xs text-slate-500">
              Recent errors ({data.failures.length})
            </summary>
            <ul className="mt-2 space-y-1">
              {data.failures.map((f, i) => (
                <li key={i} className="font-mono text-[11px] text-slate-600">
                  {new Date(f.checked_at).toLocaleString("en-ZA", {
                    timeZone: "Africa/Johannesburg",
                  })}{" "}
                  · {f.source_name} · {f.last_error}
                </li>
              ))}
            </ul>
          </details>
        )}
      </section>
    </div>
  );
}

function Kpi({
  label,
  value,
  sub,
  good,
  target,
}: {
  label: string;
  value: string;
  sub: string;
  good?: boolean;
  target?: string;
}) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3">
      <div className="text-xs uppercase tracking-wide text-slate-400">{label}</div>
      <div
        className={`mt-1 text-2xl font-bold ${
          good === undefined ? "" : good ? "text-emerald-600" : "text-amber-600"
        }`}
      >
        {value}
      </div>
      <div className="text-xs text-slate-500">{sub}</div>
      {target && <div className="text-[11px] text-slate-400">{target}</div>}
    </div>
  );
}
