"""Document processor (Blueprint §6): fetch -> hash-store -> extract -> enrich.

process_pending() walks tender_documents rows that have no content_hash yet
(i.e. registered by an adapter but never fetched), downloads each politely,
stores by content hash, extracts text + fields, and enriches the parent
tender — WITHOUT ever overriding higher-confidence values:

    enrichment rule: a document-extracted field is applied only when the
    tender's current provenance for that field is missing OR has lower
    confidence. High-stakes fields below the review threshold also land in
    review_queue with the matched evidence snippet (§6).

This is deterministic pipeline code — no AI, no crawl decisions (§6 doctrine).
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tenderza.adapters.base import USER_AGENT
from tenderza.documents.extract import ExtractedFields, extract_fields
from tenderza.documents.storage import ObjectStore
from tenderza.documents.text import extract_text

REVIEW_THRESHOLD = 0.80
HIGH_STAKES = {"closing_at", "value_estimated", "cidb_grades", "bbee_level"}

# Only these columns may be enriched from documents; anything else in the
# extractor output is stored as evidence but never written to tenders.
_ENRICHABLE = {
    "closing_at": "closing_at",
    "briefing_at": "briefing_at",
    "compulsory_briefing": "compulsory_briefing",
    "value_estimated": "value_estimated",
}


@dataclass
class ProcessOutcome:
    doc_id: str
    tender_id: str
    stored: bool = False
    deduped: bool = False          # same bytes seen before (content hash hit)
    needs_ocr: bool = False
    fields_found: int = 0
    enriched: list[str] | None = None
    queued_for_review: list[str] | None = None
    error: str | None = None


def fetch_document(url: str, *, timeout: float = 60.0) -> bytes:
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=timeout,
                      follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.content


def process_pending(conn: psycopg.Connection, store: ObjectStore,
                    *, limit: int = 20) -> list[ProcessOutcome]:
    """Fetch + process all unfetched tender documents."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, tender_id, doc_url, filename FROM tender_documents
            WHERE content_hash IS NULL
            ORDER BY id
            LIMIT %s
            """,
            (limit,),
        )
        docs = cur.fetchall()

    outcomes = []
    for doc in docs:
        outcome = process_one(conn, store, doc)
        conn.commit()
        outcomes.append(outcome)
    return outcomes


def process_one(conn: psycopg.Connection, store: ObjectStore,
                doc: dict) -> ProcessOutcome:
    out = ProcessOutcome(doc_id=str(doc["id"]), tender_id=str(doc["tender_id"]))

    try:
        data = fetch_document(doc["doc_url"])
    except httpx.HTTPError as exc:
        out.error = f"{type(exc).__name__}: {exc}"
        return out

    suffix = "." + doc["doc_url"].rsplit(".", 1)[-1][:5] if "." in doc["doc_url"][-6:] \
        else (".pdf" if data[:5] == b"%PDF-" else "")
    stored = store.put(data, suffix=suffix)
    out.stored, out.deduped = True, stored.already_existed

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE tender_documents SET content_hash = %s, object_key = %s, "
            "fetched_at = now() WHERE id = %s",
            (stored.digest, stored.key, doc["id"]),
        )

    # Text: reuse the cached extraction when the same bytes were seen before.
    cached = store.get_text(stored.digest) if stored.already_existed else None
    if cached is not None:
        text = cached
    else:
        et = extract_text(data, filename=doc.get("filename") or "")
        out.needs_ocr = et.needs_ocr
        if not et.usable:
            return out                       # OCR stage picks these up later
        text = et.text
        store.put_text(stored.digest, text)

    fields = extract_fields(text)
    out.fields_found = len(fields.as_dict())
    if out.fields_found:
        out.enriched, out.queued_for_review = _enrich_tender(
            conn, str(doc["tender_id"]), fields, source_note=f"doc:{stored.digest[:12]}"
        )
    return out


def _enrich_tender(conn: psycopg.Connection, tender_id: str,
                   fields: ExtractedFields,
                   *, source_note: str) -> tuple[list[str], list[str]]:
    """Apply extracted fields under the confidence rule; queue high-stakes
    low-confidence ones for human review with evidence."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT field_provenance FROM tenders WHERE id = %s",
                    (tender_id,))
        row = cur.fetchone()
        if row is None:
            return [], []
        provenance = dict(row["field_provenance"] or {})

    enriched: list[str] = []
    queued: list[str] = []
    updates: dict[str, object] = {}

    for name, ex in fields.as_dict().items():
        current_conf = (provenance.get(name) or {}).get("confidence", 0.0)
        if name in _ENRICHABLE and ex["confidence"] > current_conf:
            updates[_ENRICHABLE[name]] = ex["value"]
            provenance[name] = {"source": "DERIVED", "source_id": source_note,
                                "confidence": ex["confidence"]}
            enriched.append(name)
        if name in HIGH_STAKES and ex["confidence"] < REVIEW_THRESHOLD:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO review_queue (tender_id, field, value, confidence)
                    SELECT %s, %s, %s, %s
                    WHERE NOT EXISTS (
                        SELECT 1 FROM review_queue
                        WHERE tender_id = %s AND field = %s AND resolved_at IS NULL
                    )
                    """,
                    (tender_id, name, Jsonb(ex), ex["confidence"],
                     tender_id, name),
                )
            queued.append(name)

    if updates or enriched:
        sets = ", ".join(f"{col} = %s" for col in updates)
        params: list[object] = list(updates.values())
        sql = f"UPDATE tenders SET field_provenance = %s{', ' + sets if sets else ''} WHERE id = %s"  # noqa: S608
        with conn.cursor() as cur:
            cur.execute(sql, [Jsonb(provenance), *params, tender_id])

    return enriched, queued
