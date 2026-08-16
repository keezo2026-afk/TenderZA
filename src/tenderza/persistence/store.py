"""TenderStore: canonical tenders + raw OCDS records -> Postgres (§9, §10, §11).

Upsert semantics:
* Lookup by fingerprint (§7). New fingerprint -> INSERT + version 1.
* Existing fingerprint -> diff tracked fields (§9); if changed, UPDATE and
  append a tender_versions row with the field diffs and a change label.
* Raw OCDS releases are archived verbatim into ocds_records, keyed
  (ocid, release_id) — the audit layer of §10.2.6. Conflict -> ignore
  (releases are immutable snapshots).
* Buyers are resolved/created in organisations, and the seen alias is
  recorded in organisation_aliases for future entity resolution (§7).
* Review items land in review_queue (§6).

The store takes an open psycopg connection; transaction control belongs to
the caller (scripts commit per batch).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tenderza.persistence.diff import classify_change, diff_tender_fields
from tenderza.pipeline.dedupe import fingerprint, natural_key
from tenderza.pipeline.entity_resolution import normalize_org_name
from tenderza.pipeline.normalizer import CanonicalTender
from tenderza.pipeline.status import compute_status


@dataclass
class UpsertResult:
    tender_id: str
    created: bool
    changed: bool
    version_no: int
    change_kind: str | None = None


def _tender_fields(t: CanonicalTender, status: str) -> dict[str, Any]:
    """The tracked-field dict used both for INSERT and for diffing (§9)."""
    return {
        "title": t.title,
        "tender_number": t.tender_number,
        "description": t.description,
        "buyer_name": t.buyer_name,
        "province": t.province,
        "status": status,
        "published_at": t.published_at,
        "closing_at": t.closing_at,
        "briefing_at": t.briefing_at,
        "compulsory_briefing": t.compulsory_briefing,
        "value_estimated": t.value_estimated,
        "currency": t.currency,
    }


class TenderStore:
    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    # -- organisations -----------------------------------------------------

    def resolve_or_create_buyer(self, tender: CanonicalTender) -> str | None:
        """Alias-lookup the buyer; create org + alias when unknown (§7)."""
        if not tender.buyer_name:
            return None
        alias_key = normalize_org_name(tender.buyer_name)
        if not alias_key:
            return None

        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT org_id FROM organisation_aliases WHERE alias = %s AND active",
                (alias_key,),
            )
            row = cur.fetchone()
            if row:
                return str(row[0])

            # Unknown buyer: create a minimal organisation (type refined
            # later by the reconciliation step) and register the alias.
            cur.execute(
                """
                INSERT INTO organisations (name, type, province)
                VALUES (%s, 'PUBLIC_ENTITY', %s)
                RETURNING id
                """,
                (tender.buyer_name, tender.province),
            )
            org_id = str(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO organisation_aliases (org_id, alias)
                VALUES (%s, %s)
                ON CONFLICT (alias) DO NOTHING
                """,
                (org_id, alias_key),
            )
            return org_id

    def add_alias(self, org_id: str, alias: str) -> None:
        key = normalize_org_name(alias)
        if not key:
            return
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO organisation_aliases (org_id, alias)
                VALUES (%s, %s) ON CONFLICT (alias) DO NOTHING
                """,
                (org_id, key),
            )

    # -- OCDS mirror layer (§10.2.6) ----------------------------------------

    def archive_ocds_release(self, release: dict[str, Any]) -> bool:
        """Archive a raw release verbatim. Returns True if newly stored."""
        ocid = release.get("ocid")
        if not ocid:
            return False
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ocds_records (ocid, release_id, stage, raw)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (ocid, release_id) DO NOTHING
                RETURNING id
                """,
                (
                    ocid,
                    release.get("id"),
                    ",".join(release.get("tag") or []) or None,
                    Jsonb(release),
                ),
            )
            return cur.fetchone() is not None

    # -- tenders (§9 upsert + versioning) ------------------------------------

    _EXISTING_COLS = """
        SELECT id, title, tender_number, description, buyer_name,
               province, status::text AS status, published_at,
               closing_at, briefing_at, compulsory_briefing,
               value_estimated, currency, source_urls
        FROM tenders_with_buyer
    """

    def upsert_tender(self, tender: CanonicalTender) -> UpsertResult:
        fp = fingerprint(tender)
        nk = natural_key(tender)
        buyer_id = tender.buyer_org_id or self.resolve_or_create_buyer(tender)
        status = compute_status(tender)
        fields = _tender_fields(tender, status)

        with self.conn.cursor(row_factory=dict_row) as cur:
            # Lookup 1: natural key (buyer + normalized tender number) —
            # closing-date-independent, so date EXTENSIONS update the same
            # record instead of forking a new one (§9).
            existing = None
            if nk:
                cur.execute(
                    self._EXISTING_COLS + " WHERE natural_key = %s", (nk,)
                )
                existing = cur.fetchone()
            # Lookup 2: fingerprint — catches number-less tenders.
            if existing is None:
                cur.execute(
                    self._EXISTING_COLS + " WHERE fingerprint = %s", (fp,)
                )
                existing = cur.fetchone()

            if existing is None:
                return self._insert(cur, tender, fp, nk, buyer_id, fields)
            return self._update(cur, tender, existing, fp, buyer_id, fields)

    def _insert(
        self, cur, tender: CanonicalTender, fp: str, nk: str | None, buyer_id, fields
    ) -> UpsertResult:
        cur.execute(
            """
            INSERT INTO tenders (
                tender_number, normalized_tender_number, fingerprint, natural_key,
                title, description, buyer_id, province, status, published_at,
                closing_at, briefing_at, compulsory_briefing, value_estimated,
                currency, requirements, categories, contact, original_url,
                source_urls, field_provenance, ocds_ocid, authority_score
            ) VALUES (
                %(tender_number)s, %(norm)s, %(fp)s, %(nk)s, %(title)s,
                %(description)s, %(buyer_id)s, %(province)s, %(status)s::tender_status,
                %(published_at)s, %(closing_at)s, %(briefing_at)s,
                %(compulsory_briefing)s, %(value_estimated)s, %(currency)s,
                %(requirements)s, %(categories)s, %(contact)s, %(original_url)s,
                %(source_urls)s, %(field_provenance)s, %(ocid)s,
                %(authority_score)s
            )
            RETURNING id
            """,
            {
                **{k: fields[k] for k in (
                    "tender_number", "title", "description", "province",
                    "status", "published_at", "closing_at", "briefing_at",
                    "compulsory_briefing", "value_estimated", "currency",
                )},
                "norm": tender.normalized_tender_number or None,
                "fp": fp,
                "nk": nk,
                "buyer_id": buyer_id,
                "requirements": Jsonb(tender.requirements),
                "categories": Jsonb(tender.categories),
                "contact": Jsonb({}),
                "original_url": tender.source_url,
                "source_urls": Jsonb(tender.source_urls),
                "field_provenance": Jsonb(tender.field_provenance),
                "ocid": tender.raw.get("ocid"),
                "authority_score": tender.authority_score,
            },
        )
        tender_id = str(cur.fetchone()["id"])

        cur.execute(
            """
            INSERT INTO tender_versions (tender_id, version_no, changes, change_kind)
            VALUES (%s, 1, %s, 'CREATED')
            """,
            (tender_id, Jsonb({})),
        )
        self._store_documents(cur, tender_id, tender)
        self._enqueue_reviews(cur, tender_id, tender)
        return UpsertResult(tender_id, created=True, changed=False,
                            version_no=1, change_kind="CREATED")

    def _update(
        self, cur, tender: CanonicalTender, existing, fp: str, buyer_id, fields
    ) -> UpsertResult:
        tender_id = str(existing["id"])
        changes = diff_tender_fields(dict(existing), fields)

        # Merge attribution regardless of field changes (§7).
        merged_urls = list(existing["source_urls"] or [])
        new_urls = [u for u in tender.source_urls if u not in merged_urls]
        merged_urls.extend(new_urls)

        if not changes and not new_urls:
            cur.execute("SELECT max(version_no) AS v FROM tender_versions WHERE tender_id = %s",
                        (tender_id,))
            v = cur.fetchone()["v"] or 1
            return UpsertResult(tender_id, created=False, changed=False, version_no=v)

        cur.execute(
            """
            UPDATE tenders SET
                fingerprint = %(fp)s,
                title = %(title)s,
                tender_number = coalesce(%(tender_number)s, tender_number),
                description = coalesce(%(description)s, description),
                buyer_id = coalesce(%(buyer_id)s, buyer_id),
                province = coalesce(%(province)s, province),
                status = %(status)s::tender_status,
                published_at = coalesce(%(published_at)s, published_at),
                closing_at = coalesce(%(closing_at)s, closing_at),
                briefing_at = coalesce(%(briefing_at)s, briefing_at),
                compulsory_briefing = coalesce(%(compulsory_briefing)s, compulsory_briefing),
                value_estimated = coalesce(%(value_estimated)s, value_estimated),
                currency = %(currency)s,
                source_urls = %(source_urls)s,
                field_provenance = %(field_provenance)s,
                -- §12: monotonic. Seeing the same tender on a low-authority
                -- aggregator must not demote a notice we found on the
                -- official portal.
                authority_score = greatest(
                    authority_score, %(authority_score)s
                )
            WHERE id = %(id)s
            """,
            {
                **fields,
                "fp": fp,
                "buyer_id": buyer_id,
                "source_urls": Jsonb(merged_urls),
                "field_provenance": Jsonb(tender.field_provenance),
                "authority_score": tender.authority_score,
                "id": tender_id,
            },
        )

        change_kind = classify_change(changes) if changes else None
        version_no = 1
        if changes:
            cur.execute(
                "SELECT coalesce(max(version_no), 0) + 1 AS v FROM tender_versions "
                "WHERE tender_id = %s",
                (tender_id,),
            )
            version_no = cur.fetchone()["v"]
            cur.execute(
                """
                INSERT INTO tender_versions (tender_id, version_no, changes, change_kind)
                VALUES (%s, %s, %s, %s)
                """,
                (tender_id, version_no, Jsonb(changes), change_kind),
            )

        self._store_documents(cur, tender_id, tender)
        self._enqueue_reviews(cur, tender_id, tender)
        return UpsertResult(tender_id, created=False, changed=bool(changes),
                            version_no=version_no, change_kind=change_kind)

    # -- helpers --------------------------------------------------------------

    def _store_documents(self, cur, tender_id: str, tender: CanonicalTender) -> None:
        for doc in tender.documents:
            cur.execute(
                """
                INSERT INTO tender_documents (tender_id, doc_url, filename)
                SELECT %s, %s, %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM tender_documents
                    WHERE tender_id = %s AND doc_url = %s
                )
                """,
                (tender_id, doc["url"], doc.get("filename"), tender_id, doc["url"]),
            )

    def _enqueue_reviews(self, cur, tender_id: str, tender: CanonicalTender) -> None:
        for item in tender.review_items:
            cur.execute(
                """
                INSERT INTO review_queue (tender_id, field, value, confidence)
                SELECT %s, %s, %s, %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM review_queue
                    WHERE tender_id = %s AND field = %s AND resolved_at IS NULL
                )
                """,
                (
                    tender_id,
                    item["field"],
                    Jsonb(json.loads(json.dumps(item.get("value"), default=str))),
                    item["confidence"],
                    tender_id,
                    item["field"],
                ),
            )

    # -- stats (for the ingest script's report) --------------------------------

    def counts(self) -> dict[str, int]:
        with self.conn.cursor() as cur:
            out: dict[str, int] = {}
            for table in ("tenders", "ocds_records", "tender_versions",
                          "organisations", "review_queue"):
                cur.execute(f"SELECT count(*) FROM {table}")  # noqa: S608 — fixed list
                out[table] = cur.fetchone()[0]
            return out
