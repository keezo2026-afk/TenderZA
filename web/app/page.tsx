"use client";

import { useCallback, useEffect, useState } from "react";
import {
  PROVINCES,
  STATUSES,
  searchTenders,
  getStats,
  type SearchResponse,
  type Stats,
} from "@/lib/api";
import TenderCard from "@/components/TenderCard";

const PAGE_SIZE = 20;

export default function SearchPage() {
  const [q, setQ] = useState("");
  const [province, setProvince] = useState("");
  const [status, setStatus] = useState("");
  const [closingDays, setClosingDays] = useState("");
  const [briefingOnly, setBriefingOnly] = useState(false);
  const [offset, setOffset] = useState(0);

  const [data, setData] = useState<SearchResponse | null>(null);
  const [stats, setStats] = useState<Stats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const runSearch = useCallback(
    async (nextOffset = 0) => {
      setLoading(true);
      setError(null);
      try {
        const res = await searchTenders({
          q: q || undefined,
          province: province || undefined,
          status: status || undefined,
          closing_within_days: closingDays || undefined,
          compulsory_briefing: briefingOnly ? "true" : undefined,
          offset: nextOffset,
        });
        setData(res);
        setOffset(nextOffset);
      } catch (e) {
        setError(e instanceof Error ? e.message : "search failed");
      } finally {
        setLoading(false);
      }
    },
    [q, province, status, closingDays, briefingOnly]
  );

  // Initial load + stats
  useEffect(() => {
    runSearch(0);
    getStats().then(setStats).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Re-search when dropdown filters change (text input searches on submit)
  useEffect(() => {
    runSearch(0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [province, status, closingDays, briefingOnly]);

  const totalPages = data ? Math.ceil(data.total / PAGE_SIZE) : 0;
  const page = Math.floor(offset / PAGE_SIZE) + 1;

  return (
    <div>
      {/* Stats strip */}
      {stats && (
        <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <div className="rounded-lg border border-slate-200 bg-white p-3">
            <div className="text-2xl font-bold text-emerald-600">
              {stats.open_now}
            </div>
            <div className="text-xs text-slate-500">open now</div>
          </div>
          {Object.entries(stats.by_province)
            .slice(0, 3)
            .map(([prov, count]) => (
              <div
                key={prov}
                className="rounded-lg border border-slate-200 bg-white p-3"
              >
                <div className="text-2xl font-bold">{count}</div>
                <div className="truncate text-xs text-slate-500">{prov}</div>
              </div>
            ))}
        </div>
      )}

      {/* Search form */}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          runSearch(0);
        }}
        className="rounded-lg border border-slate-200 bg-white p-4"
      >
        <div className="flex gap-2">
          <input
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder='Search tenders… e.g. cctv, "grass cutting", copper -maintenance'
            className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-emerald-500 focus:outline-none"
          />
          <button
            type="submit"
            className="rounded-md bg-emerald-600 px-5 py-2 text-sm font-semibold text-white hover:bg-emerald-700"
          >
            Search
          </button>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2 text-sm">
          <select
            value={province}
            onChange={(e) => setProvince(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5"
          >
            <option value="">All provinces</option>
            {PROVINCES.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5"
          >
            <option value="">Any status</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s.replace("_", " ")}
              </option>
            ))}
          </select>
          <select
            value={closingDays}
            onChange={(e) => setClosingDays(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5"
          >
            <option value="">Any deadline</option>
            <option value="7">Closing within 7 days</option>
            <option value="14">Closing within 14 days</option>
            <option value="30">Closing within 30 days</option>
            <option value="90">Closing within 90 days</option>
          </select>
          <label className="flex cursor-pointer items-center gap-1.5 text-slate-600">
            <input
              type="checkbox"
              checked={briefingOnly}
              onChange={(e) => setBriefingOnly(e.target.checked)}
              className="accent-emerald-600"
            />
            Compulsory briefing
          </label>
        </div>
      </form>

      {/* Results */}
      <div className="mt-5">
        {error && (
          <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700">
            {error}
          </div>
        )}
        {loading && (
          <div className="py-10 text-center text-sm text-slate-400">
            Searching…
          </div>
        )}
        {!loading && data && (
          <>
            <div className="mb-3 text-sm text-slate-500">
              {data.total} tender{data.total === 1 ? "" : "s"} found
            </div>
            <div className="space-y-3">
              {data.results.map((t) => (
                <TenderCard key={t.id} tender={t} />
              ))}
            </div>
            {data.total === 0 && (
              <div className="rounded-lg border border-dashed border-slate-300 p-10 text-center text-sm text-slate-400">
                No tenders match. Try fewer filters.
              </div>
            )}
            {totalPages > 1 && (
              <div className="mt-5 flex items-center justify-center gap-3 text-sm">
                <button
                  disabled={page <= 1}
                  onClick={() => runSearch(offset - PAGE_SIZE)}
                  className="rounded-md border border-slate-300 px-3 py-1.5 disabled:opacity-40"
                >
                  ← Prev
                </button>
                <span className="text-slate-500">
                  Page {page} of {totalPages}
                </span>
                <button
                  disabled={page >= totalPages}
                  onClick={() => runSearch(offset + PAGE_SIZE)}
                  className="rounded-md border border-slate-300 px-3 py-1.5 disabled:opacity-40"
                >
                  Next →
                </button>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
