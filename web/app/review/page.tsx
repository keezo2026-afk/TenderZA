"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";

interface ReviewItem {
  id: string;
  field: string;
  extracted: { value: unknown; confidence: number; evidence?: string } | null;
  confidence: number;
  queued_at: string;
  tender: {
    id: string;
    tender_number: string | null;
    title: string;
    buyer: string | null;
    status: string;
    current_closing_at: string | null;
    verify_at_source: string | null;
  };
}

interface ReviewStats {
  open: number;
  open_by_field: Record<string, number>;
  resolved_total: number;
}

const FIELD_LABELS: Record<string, string> = {
  closing_at: "Closing date",
  briefing_at: "Briefing date",
  compulsory_briefing: "Compulsory briefing",
  value_estimated: "Estimated value",
  bbee_level: "B-BBEE level",
  cidb_grades: "CIDB grades",
  tender_number: "Tender number",
  requirements: "Requirements",
};

function fmt(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}T/.test(value)) {
    return new Date(value).toLocaleString("en-ZA", {
      day: "numeric", month: "short", year: "numeric",
      hour: "2-digit", minute: "2-digit", timeZone: "Africa/Johannesburg",
    });
  }
  return String(value);
}

export default function ReviewPage() {
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [stats, setStats] = useState<ReviewStats | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [correcting, setCorrecting] = useState<string | null>(null);
  const [correction, setCorrection] = useState("");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [itemsRes, statsRes] = await Promise.all([
        fetch("/api/review"),
        fetch("/api/review/stats"),
      ]);
      if (!itemsRes.ok) throw new Error(`load failed: ${itemsRes.status}`);
      setItems((await itemsRes.json()).items);
      setStats(await statsRes.json());
    } catch (e) {
      setError(e instanceof Error ? e.message : "load failed");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function resolve(
    id: string,
    action: "approve" | "correct" | "reject",
    corrected_value?: string
  ) {
    setBusy(id);
    setError(null);
    try {
      const res = await fetch(`/api/review/${id}/resolve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, corrected_value }),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(detail.detail ?? `resolve failed: ${res.status}`);
      }
      setCorrecting(null);
      setCorrection("");
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "resolve failed");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div>
      <div className="mb-5 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Review queue</h1>
          <p className="text-sm text-slate-500">
            Low-confidence high-stakes fields awaiting human confirmation.
            Verified values unlock deadline alerts.
          </p>
        </div>
        {stats && (
          <div className="text-right text-sm">
            <div className="text-2xl font-bold text-amber-600">{stats.open}</div>
            <div className="text-xs text-slate-500">
              open · {stats.resolved_total} resolved
            </div>
          </div>
        )}
      </div>

      {error && (
        <div className="mb-4 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700">
          {error}
        </div>
      )}

      {items.length === 0 && !error && (
        <div className="rounded-lg border border-dashed border-slate-300 p-10 text-center text-sm text-slate-400">
          Queue is empty — every extracted field is verified. 🎉
        </div>
      )}

      <div className="space-y-4">
        {items.map((item) => (
          <div
            key={item.id}
            className="rounded-lg border border-slate-200 bg-white p-4"
          >
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2 text-xs">
                  <span className="rounded bg-amber-100 px-2 py-0.5 font-semibold text-amber-800">
                    {FIELD_LABELS[item.field] ?? item.field}
                  </span>
                  <span className="text-slate-400">
                    confidence {(item.confidence * 100).toFixed(0)}%
                  </span>
                </div>
                <Link
                  href={`/tender/${item.tender.id}`}
                  className="mt-1 block font-medium hover:text-emerald-700"
                >
                  {item.tender.tender_number
                    ? `${item.tender.tender_number} — `
                    : ""}
                  {item.tender.title}
                </Link>
                <div className="text-sm text-slate-500">
                  {item.tender.buyer ?? "Unknown buyer"}
                </div>
              </div>
              <div className="text-right text-sm">
                <div className="text-xs text-slate-500">Extracted value</div>
                <div className="font-mono font-medium">
                  {fmt(item.extracted?.value)}
                </div>
                {item.tender.current_closing_at &&
                  item.field === "closing_at" && (
                    <div className="text-xs text-slate-400">
                      current: {fmt(item.tender.current_closing_at)}
                    </div>
                  )}
              </div>
            </div>

            {item.extracted?.evidence && (
              <blockquote className="mt-3 border-l-2 border-amber-300 bg-amber-50/50 px-3 py-2 font-mono text-xs text-slate-600">
                “…{item.extracted.evidence}…”
              </blockquote>
            )}

            <div className="mt-3 flex flex-wrap items-center gap-2 text-sm">
              <button
                disabled={busy === item.id}
                onClick={() => resolve(item.id, "approve")}
                className="rounded-md bg-emerald-600 px-3 py-1.5 font-semibold text-white hover:bg-emerald-700 disabled:opacity-50"
              >
                ✓ Approve
              </button>
              {correcting === item.id ? (
                <span className="flex items-center gap-2">
                  <input
                    autoFocus
                    value={correction}
                    onChange={(e) => setCorrection(e.target.value)}
                    placeholder={
                      item.field.endsWith("_at")
                        ? "2026-09-30T11:00:00+02:00"
                        : "corrected value"
                    }
                    className="rounded-md border border-slate-300 px-2 py-1.5 font-mono text-xs"
                    size={28}
                  />
                  <button
                    disabled={busy === item.id || !correction}
                    onClick={() => resolve(item.id, "correct", correction)}
                    className="rounded-md bg-blue-600 px-3 py-1.5 font-semibold text-white hover:bg-blue-700 disabled:opacity-50"
                  >
                    Save
                  </button>
                  <button
                    onClick={() => setCorrecting(null)}
                    className="text-slate-400 hover:text-slate-700"
                  >
                    cancel
                  </button>
                </span>
              ) : (
                <button
                  disabled={busy === item.id}
                  onClick={() => {
                    setCorrecting(item.id);
                    setCorrection("");
                  }}
                  className="rounded-md border border-slate-300 px-3 py-1.5 hover:border-blue-400"
                >
                  ✎ Correct
                </button>
              )}
              <button
                disabled={busy === item.id}
                onClick={() => resolve(item.id, "reject")}
                className="rounded-md border border-slate-300 px-3 py-1.5 text-slate-500 hover:border-red-300 hover:text-red-600"
              >
                ✕ Reject
              </button>
              {item.tender.verify_at_source && (
                <a
                  href={item.tender.verify_at_source}
                  target="_blank"
                  rel="noreferrer"
                  className="ml-auto text-xs text-slate-400 underline hover:text-slate-700"
                >
                  verify at source ↗
                </a>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
