"use client";

import { use, useEffect, useState } from "react";
import Link from "next/link";
import { getTender, type TenderDetail } from "@/lib/api";
import StatusBadge from "@/components/StatusBadge";
import { formatDate } from "@/components/TenderCard";

function ProvenanceDot({
  prov,
}: {
  prov?: { source: string; confidence: number };
}) {
  if (!prov) return null;
  const verified = prov.confidence >= 0.8 && prov.source === "SOURCE";
  return (
    <span
      title={`${prov.source} · confidence ${(prov.confidence * 100).toFixed(0)}%`}
      className={`ml-1 inline-block h-2 w-2 rounded-full align-middle ${
        verified ? "bg-emerald-500" : "bg-amber-400"
      }`}
    />
  );
}

export default function TenderPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const [tender, setTender] = useState<TenderDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getTender(id)
      .then(setTender)
      .catch((e) => setError(e.message));
  }, [id]);

  if (error)
    return (
      <div className="rounded-md border border-red-200 bg-red-50 p-4 text-sm text-red-700">
        {error} — <Link href="/" className="underline">back to search</Link>
      </div>
    );
  if (!tender)
    return (
      <div className="py-10 text-center text-sm text-slate-400">Loading…</div>
    );

  const d = tender.dates;
  const prov = tender.field_provenance ?? {};

  return (
    <div>
      <Link href="/" className="text-sm text-slate-500 hover:text-slate-900">
        ← Back to search
      </Link>

      <div className="mt-3 rounded-lg border border-slate-200 bg-white p-5">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          {tender.tender_number && (
            <span className="font-mono font-semibold">{tender.tender_number}</span>
          )}
          <StatusBadge status={tender.status} />
        </div>
        <h1 className="mt-2 text-xl font-semibold">{tender.title}</h1>
        <p className="mt-1 text-slate-600">
          {tender.buyer ?? "Unknown buyer"}
          {tender.province ? ` · ${tender.province}` : ""}
        </p>

        {/* Key dates */}
        <div className="mt-5 grid gap-3 sm:grid-cols-3">
          <div className="rounded-md bg-slate-50 p-3">
            <div className="text-xs text-slate-500">Published</div>
            <div className="mt-0.5 text-sm font-medium">
              {formatDate(d.published_at)}
              <ProvenanceDot prov={prov["published_at"]} />
            </div>
          </div>
          <div
            className={`rounded-md p-3 ${
              d.closing_verified ? "bg-slate-50" : "bg-amber-50"
            }`}
          >
            <div className="text-xs text-slate-500">Closes</div>
            <div className="mt-0.5 text-sm font-medium">
              {formatDate(d.closing_at)}
              <ProvenanceDot prov={prov["closing_at"]} />
            </div>
            {!d.closing_verified && (
              <a
                href={d.verify_at_source ?? tender.original_url ?? "#"}
                target="_blank"
                rel="noreferrer"
                className="mt-1 block text-xs font-medium text-amber-700 underline"
              >
                Unverified — verify at source
              </a>
            )}
          </div>
          <div
            className={`rounded-md p-3 ${
              d.compulsory_briefing ? "bg-orange-50" : "bg-slate-50"
            }`}
          >
            <div className="text-xs text-slate-500">
              Briefing{d.compulsory_briefing ? " (COMPULSORY)" : ""}
            </div>
            <div className="mt-0.5 text-sm font-medium">
              {formatDate(d.briefing_at)}
              <ProvenanceDot prov={prov["briefing_at"]} />
            </div>
            {d.compulsory_briefing && (
              <div className="mt-1 text-xs font-medium text-orange-700">
                Missing this briefing disqualifies your bid.
              </div>
            )}
          </div>
        </div>

        {tender.description && tender.description !== tender.title && (
          <div className="mt-5">
            <h2 className="text-sm font-semibold text-slate-700">Description</h2>
            <p className="mt-1 whitespace-pre-line text-sm text-slate-600">
              {tender.description}
            </p>
          </div>
        )}

        {/* Documents */}
        {tender.documents.length > 0 && (
          <div className="mt-5">
            <h2 className="text-sm font-semibold text-slate-700">Documents</h2>
            <ul className="mt-1 space-y-1">
              {tender.documents.map((doc) => (
                <li key={doc.doc_url}>
                  <a
                    href={doc.doc_url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-sm text-emerald-700 underline hover:text-emerald-900"
                  >
                    📄 {doc.filename ?? doc.doc_url}
                  </a>
                </li>
              ))}
            </ul>
            <p className="mt-1 text-xs text-slate-400">
              Documents open at the official source — we never re-host them.
            </p>
          </div>
        )}

        {/* Version history */}
        {tender.versions.length > 0 && (
          <div className="mt-5">
            <h2 className="text-sm font-semibold text-slate-700">History</h2>
            <ul className="mt-1 space-y-1 text-sm text-slate-600">
              {tender.versions.map((v) => (
                <li key={v.version_no} className="flex gap-2">
                  <span className="text-slate-400">v{v.version_no}</span>
                  <span className="font-medium">
                    {v.change_kind ?? "UPDATED"}
                  </span>
                  <span className="text-slate-400">
                    {formatDate(v.detected_at)}
                  </span>
                  {Object.keys(v.changes).length > 0 && (
                    <span className="text-xs text-slate-400">
                      ({Object.keys(v.changes).join(", ")})
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Source attribution — §17.1 */}
        <div className="mt-6 border-t border-slate-100 pt-4">
          <h2 className="text-sm font-semibold text-slate-700">Sources</h2>
          <ul className="mt-1 space-y-1">
            {tender.source_urls.map((url) => (
              <li key={url}>
                <a
                  href={url}
                  target="_blank"
                  rel="noreferrer"
                  className="break-all text-xs text-slate-500 underline hover:text-slate-800"
                >
                  {url}
                </a>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}
