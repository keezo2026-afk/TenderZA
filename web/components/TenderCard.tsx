import Link from "next/link";
import type { Tender } from "@/lib/api";
import { hasMatch, plainText, renderHighlight } from "@/lib/highlight";
import StatusBadge from "./StatusBadge";

function daysUntil(iso: string | null): number | null {
  if (!iso) return null;
  const ms = new Date(iso).getTime() - Date.now();
  return Math.ceil(ms / 86_400_000);
}

export function formatDate(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString("en-ZA", {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "Africa/Johannesburg",
  });
}

export default function TenderCard({ tender }: { tender: Tender }) {
  const days = daysUntil(tender.dates.closing_at);

  return (
    <Link
      href={`/tender/${tender.id}`}
      className="block rounded-lg border border-slate-200 bg-white p-4 transition hover:border-emerald-400 hover:shadow-sm"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
            {tender.tender_number && (
              <span className="font-mono font-medium text-slate-700">
                {tender.tender_number}
              </span>
            )}
            <StatusBadge status={tender.status} />
            {tender.dates.compulsory_briefing && (
              <span className="rounded bg-orange-100 px-2 py-0.5 text-xs font-semibold text-orange-800">
                ⚠ Compulsory briefing
              </span>
            )}
          </div>
          <h3
            className="mt-1 line-clamp-2 font-medium text-slate-900"
            // The rendered title may contain <mark> elements; keep the plain
            // text available to assistive tech and native tooltips.
            title={plainText(tender.highlight?.title) || tender.title}
          >
            {renderHighlight(tender.highlight?.title, `t-${tender.id}`) ??
              tender.title}
          </h3>
          <p className="mt-1 text-sm text-slate-500">
            {tender.buyer ?? "Unknown buyer"}
            {tender.province ? ` · ${tender.province}` : ""}
          </p>
          {hasMatch(tender.highlight?.snippet) && (
            <p className="mt-2 line-clamp-2 text-sm text-slate-600">
              {renderHighlight(tender.highlight?.snippet, `s-${tender.id}`)}
            </p>
          )}
        </div>
        <div className="shrink-0 text-right text-sm">
          <div className="text-slate-500">Closes</div>
          <div className="font-medium">
            {formatDate(tender.dates.closing_at)}
          </div>
          {tender.dates.closing_verified ? (
            days !== null &&
            days >= 0 && (
              <div
                className={`text-xs ${
                  days <= 3 ? "font-semibold text-amber-600" : "text-slate-400"
                }`}
              >
                {days === 0 ? "today" : `in ${days} day${days === 1 ? "" : "s"}`}
              </div>
            )
          ) : (
            <div className="text-xs font-medium text-amber-600">
              unverified — check source
            </div>
          )}
        </div>
      </div>
    </Link>
  );
}
