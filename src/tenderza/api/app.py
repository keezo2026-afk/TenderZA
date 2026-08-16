"""FastAPI read layer (Blueprint §12, §18).

Endpoints (v1 — P1 "basic search/filter UI" deliverable):
    GET /health                      liveness + DB counts
    GET /tenders                     search/filter (FTS + structured filters)
    GET /tenders/{tender_id}         one tender + documents + version history
    GET /stats                       dashboard numbers (per-status/province)

Run:
    DATABASE_URL=postgresql://... uvicorn tenderza.api.app:app --host 0.0.0.0

Trust rules enforced here (§10.3): unverified closing dates are marked
closing_verified=false with a verify_at_source link; source attribution
(original_url, source_urls) is always present (§17.1).

Access control (§17): tender search/detail is public — the whole point is
open access to public procurement data. The admin surfaces are gated:
/review needs `analyst`, /ops needs `admin`, and alert management is
owner-scoped. See tenderza.auth.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)

from tenderza.api.queries import (
    build_count_query,
    build_search_query,
    shape_tender_row,
)

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    if _pool is None:
        raise HTTPException(503, "database pool not initialised")
    return _pool


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        _pool = ConnectionPool(dsn, min_size=1, max_size=10, open=True)
    yield
    if _pool is not None:
        _pool.close()
        _pool = None


app = FastAPI(
    title="TenderZA API",
    description=(
        "South African tender discovery — read API v1. "
        "Data sourced from official portals with per-field provenance; "
        "eTender OCDS data under CC BY 4.0."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# Review-queue admin endpoints (§6). Late import avoids a cycle:
# review.py needs get_pool from this module.
from tenderza.api import alerts as _alerts  # noqa: E402
from tenderza.api import auth_routes as _auth  # noqa: E402
from tenderza.api import health_dashboard as _ops  # noqa: E402
from tenderza.api import review as _review  # noqa: E402

app.include_router(_auth.router)
app.include_router(_review.router)   # analyst+
app.include_router(_alerts.router)   # mixed: signup open, rest owner/admin
app.include_router(_ops.router)      # admin only


@app.get("/health")
def health(pool: ConnectionPool = Depends(get_pool)):
    with pool.connection() as conn, conn.cursor() as cur:
        counts = {}
        for table in ("tenders", "organisations", "ocds_records", "review_queue"):
            cur.execute(f"SELECT count(*) FROM {table}")  # noqa: S608 — fixed list
            counts[table] = cur.fetchone()[0]
    return {"status": "ok", "counts": counts}


def _query_vector(q: str | None) -> str | None:
    """Embed the query for semantic recall, or None when unavailable (§12).

    Never fatal: if the embedder errors (model down, rate limit), search must
    degrade to keyword-only rather than 500. A search engine that returns
    slightly worse results beats one that returns none.
    """
    if not q:
        return None
    from tenderza.search import semantic

    if not semantic.is_enabled():
        return None
    try:
        embedder = semantic.active_embedder()
        return semantic.to_pgvector(embedder.embed([q])[0])
    except Exception:
        log.warning("query embedding failed; falling back to keyword search",
                    exc_info=True)
        return None


@app.get("/tenders")
def search_tenders(
    q: str | None = Query(None, description="Full-text query (websearch syntax)"),
    province: str | None = Query(None),
    status: str | None = Query(None),
    buyer: str | None = Query(None, description="Buyer name substring"),
    closing_within_days: int | None = Query(None, ge=0, le=365),
    compulsory_briefing: bool | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    highlight: bool = Query(
        True, description="Return <mark>-highlighted title/snippet (§12)"
    ),
    pool: ConnectionPool = Depends(get_pool),
):
    try:
        sql, params = build_search_query(
            q=q, province=province, status=status, buyer=buyer,
            closing_within_days=closing_within_days,
            compulsory_briefing=compulsory_briefing,
            limit=limit, offset=offset,
            highlight=highlight,
            query_vector=_query_vector(q),
        )
        count_sql, count_params = build_count_query(
            q=q, province=province, status=status, buyer=buyer,
            closing_within_days=closing_within_days,
            compulsory_briefing=compulsory_briefing,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        with conn.cursor() as cur:
            cur.execute(count_sql, count_params)
            total = cur.fetchone()[0]

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "results": [shape_tender_row(r) for r in rows],
    }


@app.get("/tenders/{tender_id}")
def get_tender(tender_id: str, pool: ConnectionPool = Depends(get_pool)):
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        try:
            cur.execute(
                """
                SELECT t.id, t.tender_number, t.title, t.description,
                       t.province, t.status::text AS status, t.published_at,
                       t.closing_at, t.briefing_at, t.compulsory_briefing,
                       t.value_estimated, t.currency, t.original_url,
                       t.source_urls, t.field_provenance, t.requirements,
                       o.name AS buyer_name
                FROM tenders t
                LEFT JOIN organisations o ON o.id = t.buyer_id
                WHERE t.id = %s
                """,
                (tender_id,),
            )
        except psycopg.errors.InvalidTextRepresentation as exc:
            raise HTTPException(404, "tender not found") from exc
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, "tender not found")

        cur.execute(
            "SELECT doc_url, filename, content_hash FROM tender_documents "
            "WHERE tender_id = %s",
            (tender_id,),
        )
        documents = cur.fetchall()

        cur.execute(
            "SELECT version_no, changes, change_kind, detected_at "
            "FROM tender_versions WHERE tender_id = %s ORDER BY version_no",
            (tender_id,),
        )
        versions = [
            {
                "version_no": v["version_no"],
                "changes": v["changes"],
                "change_kind": v["change_kind"],
                "detected_at": v["detected_at"].isoformat(),
            }
            for v in cur.fetchall()
        ]

    tender = shape_tender_row(row)
    tender["requirements"] = row["requirements"]
    tender["documents"] = documents
    tender["versions"] = versions
    tender["field_provenance"] = row["field_provenance"]
    return tender


@app.get("/stats")
def stats(pool: ConnectionPool = Depends(get_pool)):
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status::text, count(*) FROM tenders GROUP BY status ORDER BY 2 DESC"
        )
        by_status = dict(cur.fetchall())
        cur.execute(
            "SELECT coalesce(province, 'Unspecified'), count(*) FROM tenders "
            "GROUP BY 1 ORDER BY 2 DESC"
        )
        by_province = dict(cur.fetchall())
        cur.execute(
            "SELECT count(*) FROM tenders WHERE status IN ('OPEN', 'CLOSING_SOON')"
        )
        open_now = cur.fetchone()[0]
    return {"open_now": open_now, "by_status": by_status, "by_province": by_province}
