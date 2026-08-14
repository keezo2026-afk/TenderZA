// TenderZA API client — talks to the FastAPI backend via the /api rewrite.

export interface TenderDates {
  published_at: string | null;
  closing_at: string | null;
  closing_verified: boolean;
  verify_at_source: string | null;
  briefing_at: string | null;
  compulsory_briefing: boolean | null;
}

export interface Tender {
  id: string;
  tender_number: string | null;
  title: string;
  description: string | null;
  buyer: string | null;
  province: string | null;
  status: string;
  dates: TenderDates;
  value_estimated: number | null;
  currency: string;
  original_url: string | null;
  source_urls: string[];
}

export interface TenderDetail extends Tender {
  requirements: Record<string, unknown>;
  documents: { doc_url: string; filename: string | null; content_hash: string | null }[];
  versions: {
    version_no: number;
    changes: Record<string, { old: unknown; new: unknown }>;
    change_kind: string | null;
    detected_at: string;
  }[];
  field_provenance: Record<
    string,
    { source: string; source_id: string; confidence: number }
  >;
}

export interface SearchResponse {
  total: number;
  limit: number;
  offset: number;
  results: Tender[];
}

export interface Stats {
  open_now: number;
  by_status: Record<string, number>;
  by_province: Record<string, number>;
}

export const PROVINCES = [
  "Eastern Cape",
  "Free State",
  "Gauteng",
  "KwaZulu-Natal",
  "Limpopo",
  "Mpumalanga",
  "North West",
  "Northern Cape",
  "Western Cape",
];

export const STATUSES = [
  "OPEN",
  "CLOSING_SOON",
  "CLOSED",
  "EXTENDED",
  "CANCELLED",
  "AWARDED",
  "RE_ADVERTISED",
  "UNKNOWN",
];

export interface SearchParams {
  q?: string;
  province?: string;
  status?: string;
  closing_within_days?: string;
  compulsory_briefing?: string;
  offset?: number;
}

export async function searchTenders(params: SearchParams): Promise<SearchResponse> {
  const qs = new URLSearchParams();
  if (params.q) qs.set("q", params.q);
  if (params.province) qs.set("province", params.province);
  if (params.status) qs.set("status", params.status);
  if (params.closing_within_days) qs.set("closing_within_days", params.closing_within_days);
  if (params.compulsory_briefing) qs.set("compulsory_briefing", params.compulsory_briefing);
  qs.set("limit", "20");
  qs.set("offset", String(params.offset ?? 0));
  const res = await fetch(`/api/tenders?${qs}`);
  if (!res.ok) throw new Error(`search failed: ${res.status}`);
  return res.json();
}

export async function getTender(id: string): Promise<TenderDetail> {
  const res = await fetch(`/api/tenders/${id}`);
  if (!res.ok) throw new Error(`tender not found: ${res.status}`);
  return res.json();
}

export async function getStats(): Promise<Stats> {
  const res = await fetch(`/api/stats`);
  if (!res.ok) throw new Error(`stats failed: ${res.status}`);
  return res.json();
}
