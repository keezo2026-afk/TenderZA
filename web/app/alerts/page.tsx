"use client";

import { useCallback, useEffect, useState } from "react";
import { PROVINCES } from "@/lib/api";
import { useAuth } from "@/components/AuthProvider";

interface Alert {
  id: string;
  keywords: string;
  provinces: string[];
  categories: string[];
  closing_within_days: number | null;
  active: boolean;
  notified_total: number;
  last_sent_at: string | null;
}

export default function AlertsPage() {
  const { user, authenticated } = useAuth();
  const [email, setEmail] = useState("");
  const [keywords, setKeywords] = useState("");
  const [province, setProvince] = useState("");
  const [closingDays, setClosingDays] = useState("");
  const [alerts, setAlerts] = useState<Alert[] | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Listing is owner-scoped server-side (§17): a signed-in caller may only
  // read their own alerts. Anonymous visitors can still *create* one — that
  // is the signup funnel — they just cannot enumerate anybody's list.
  const [listError, setListError] = useState<string | null>(null);

  const load = useCallback(async (em: string) => {
    if (!em) return;
    const res = await fetch(`/api/alerts?email=${encodeURIComponent(em)}`);
    if (res.ok) {
      setAlerts((await res.json()).alerts);
      setListError(null);
    } else if (res.status === 401) {
      setAlerts(null);
      setListError(
        "Sign in to manage alerts you have already created. You can still create a new one below."
      );
    } else if (res.status === 403) {
      setAlerts(null);
      setListError("You can only manage alerts for your own email address.");
    }
  }, []);

  // A signed-in user should not have to retype the address we already know.
  useEffect(() => {
    if (authenticated && user?.email) {
      setEmail(user.email);
      load(user.email);
    }
  }, [authenticated, user?.email, load]);

  async function createAlert(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const res = await fetch("/api/alerts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email,
          keywords,
          provinces: province ? [province] : [],
          closing_within_days: closingDays ? Number(closingDays) : null,
        }),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail ?? `failed: ${res.status}`);
      setMessage(
        "Alert saved. You'll get one digest email per run when new tenders match."
      );
      setKeywords("");
      await load(email);
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to save alert");
    } finally {
      setBusy(false);
    }
  }

  async function toggle(id: string) {
    const res = await fetch(`/api/alerts/${id}/toggle`, { method: "POST" });
    if (res.status === 401 || res.status === 403) {
      setError("Sign in as the owner of this alert to pause or resume it.");
      return;
    }
    await load(email);
  }

  return (
    <div className="mx-auto max-w-2xl">
      <h1 className="text-xl font-semibold">Email alerts</h1>
      <p className="mt-1 text-sm text-slate-500">
        Save a search and get a digest email when new matching tenders arrive.
        Deadline filters only ever use <em>verified</em> closing dates.
      </p>

      <form
        onSubmit={createAlert}
        className="mt-5 space-y-3 rounded-lg border border-slate-200 bg-white p-4"
      >
        <div>
          <label className="text-xs font-semibold text-slate-600">
            Your email
          </label>
          <input
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            onBlur={() => load(email)}
            placeholder="you@company.co.za"
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-emerald-500 focus:outline-none"
          />
        </div>
        <div>
          <label className="text-xs font-semibold text-slate-600">
            Keywords
          </label>
          <input
            value={keywords}
            onChange={(e) => setKeywords(e.target.value)}
            placeholder="e.g. cctv, security, construction"
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-emerald-500 focus:outline-none"
          />
        </div>
        <div className="flex flex-wrap gap-2">
          <select
            value={province}
            onChange={(e) => setProvince(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          >
            <option value="">All provinces</option>
            {PROVINCES.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
          <select
            value={closingDays}
            onChange={(e) => setClosingDays(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          >
            <option value="">Any deadline</option>
            <option value="7">Closing within 7 days (verified only)</option>
            <option value="14">Closing within 14 days (verified only)</option>
            <option value="30">Closing within 30 days (verified only)</option>
          </select>
          <button
            type="submit"
            disabled={busy}
            className="ml-auto rounded-md bg-emerald-600 px-5 py-2 text-sm font-semibold text-white hover:bg-emerald-700 disabled:opacity-50"
          >
            Save alert
          </button>
        </div>
      </form>

      {message && (
        <div className="mt-3 rounded-md border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800">
          {message}
        </div>
      )}
      {error && (
        <div className="mt-3 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700">
          {error}
        </div>
      )}

      {listError && (
        <div className="mt-3 rounded-md border border-slate-200 bg-white p-3 text-sm text-slate-600">
          {listError}
        </div>
      )}

      {alerts && alerts.length > 0 && (
        <div className="mt-6">
          <h2 className="text-sm font-semibold text-slate-700">
            Your alerts ({alerts.length})
          </h2>
          <div className="mt-2 space-y-2">
            {alerts.map((a) => (
              <div
                key={a.id}
                className="flex items-center justify-between rounded-lg border border-slate-200 bg-white p-3"
              >
                <div className="min-w-0 text-sm">
                  <div className="font-medium">
                    {a.keywords || "(no keywords)"}
                    {a.provinces.length > 0 && (
                      <span className="ml-2 text-xs text-slate-500">
                        {a.provinces.join(", ")}
                      </span>
                    )}
                    {a.closing_within_days && (
                      <span className="ml-2 text-xs text-slate-500">
                        closing ≤ {a.closing_within_days}d
                      </span>
                    )}
                  </div>
                  <div className="text-xs text-slate-400">
                    {a.notified_total} tender
                    {a.notified_total === 1 ? "" : "s"} notified
                    {a.last_sent_at
                      ? ` · last digest ${new Date(a.last_sent_at).toLocaleDateString("en-ZA")}`
                      : ""}
                  </div>
                </div>
                <button
                  onClick={() => toggle(a.id)}
                  className={`rounded-md px-3 py-1.5 text-xs font-semibold ${
                    a.active
                      ? "bg-emerald-100 text-emerald-800 hover:bg-emerald-200"
                      : "bg-slate-100 text-slate-500 hover:bg-slate-200"
                  }`}
                >
                  {a.active ? "Active — pause" : "Paused — resume"}
                </button>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
