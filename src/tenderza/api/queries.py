"""Search/filter SQL for the read API (Blueprint §12).

Pure query-building + row-shaping; no FastAPI imports so the module is
unit-testable without the web stack.

Ranking (§12): keyword match (ts_rank) + authority boost for official
sources arrives with ranking v2; v1 orders by FTS rank when a query is
present, else closing_at ascending (deadline-first).
"""

from __future__ import annotations

from typing import Any

VALID_STATUSES = {
    "NEW", "OPEN", "CLOSING_SOON", "CLOSED", "EXTENDED",
    "CANCELLED", "AWARDED", "RE_ADVERTISED", "UNKNOWN",
}

PROVINCES = {
    "Eastern Cape", "Free State", "Gauteng", "KwaZulu-Natal", "Limpopo",
    "Mpumalanga", "North West", "Northern Cape", "Western Cape",
}

_SELECT = """
SELECT t.id, t.tender_number, t.title, t.description,
       t.province, t.status::text AS status,
       t.published_at, t.closing_at, t.briefing_at, t.compulsory_briefing,
       t.value_estimated, t.currency, t.original_url, t.source_urls,
       t.field_provenance, o.name AS buyer_name
FROM tenders t
LEFT JOIN organisations o ON o.id = t.buyer_id
"""


def build_search_query(
    *,
    q: str | None = None,
    province: str | None = None,
    status: str | None = None,
    buyer: str | None = None,
    closing_within_days: int | None = None,
    compulsory_briefing: bool | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[str, list[Any]]:
    """Return (sql, params) for the tender search. Raises ValueError on
    invalid filter values — the API layer maps that to HTTP 422."""
    if status is not None:
        status = status.upper()
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status {status!r}; one of {sorted(VALID_STATUSES)}")
    if province is not None and province not in PROVINCES:
        raise ValueError(f"invalid province {province!r}; one of {sorted(PROVINCES)}")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be 1..100")
    if offset < 0:
        raise ValueError("offset must be >= 0")

    where: list[str] = []
    params: list[Any] = []

    if q:
        # websearch_to_tsquery: user-friendly syntax ("cctv -maintenance",
        # quoted phrases), no crash on stray operators.
        where.append("t.search_vector @@ websearch_to_tsquery('english', %s)")
        params.append(q)
    if province:
        where.append("t.province = %s")
        params.append(province)
    if status:
        where.append("t.status = %s::tender_status")
        params.append(status)
    if buyer:
        where.append("o.name ILIKE %s")
        params.append(f"%{buyer}%")
    if closing_within_days is not None:
        where.append(
            "t.closing_at IS NOT NULL AND t.closing_at BETWEEN now() "
            "AND now() + make_interval(days => %s)"
        )
        params.append(closing_within_days)
    if compulsory_briefing is not None:
        where.append("t.compulsory_briefing = %s")
        params.append(compulsory_briefing)

    sql = _SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)

    if q:
        sql += (
            " ORDER BY ts_rank(t.search_vector,"
            " websearch_to_tsquery('english', %s)) DESC, t.closing_at ASC NULLS LAST"
        )
        params.append(q)
    else:
        sql += " ORDER BY t.closing_at ASC NULLS LAST, t.published_at DESC NULLS LAST"

    sql += " LIMIT %s OFFSET %s"
    params.extend([limit, offset])
    return sql, params


def build_count_query(
    *,
    q: str | None = None,
    province: str | None = None,
    status: str | None = None,
    buyer: str | None = None,
    closing_within_days: int | None = None,
    compulsory_briefing: bool | None = None,
) -> tuple[str, list[Any]]:
    sql, params = build_search_query(
        q=q, province=province, status=status, buyer=buyer,
        closing_within_days=closing_within_days,
        compulsory_briefing=compulsory_briefing,
        limit=1, offset=0,
    )
    # Strip ORDER BY/LIMIT and wrap in count.
    body = sql.split(" ORDER BY ")[0]
    return f"SELECT count(*) FROM ({body}) sub", params[: len(params) - (3 if q else 2)]


def shape_tender_row(row: dict[str, Any]) -> dict[str, Any]:
    """DB row -> API tender object. Enforces the §10.3 trust rule: an
    unverified closing date is presented as verify-at-source."""
    provenance = row.get("field_provenance") or {}
    closing_prov = provenance.get("closing_at") or {}
    closing_verified = (closing_prov.get("confidence") or 0.0) >= 0.80
    # §10.2.5: when we repaired a publisher's value (e.g. eTenders stamping
    # SAST wall times with a "Z"), say so next to the date rather than
    # presenting the corrected instant as if it came straight from source.
    closing_note = closing_prov.get("note") if closing_prov.get("source") in (
        "DERIVED", "INFERRED"
    ) else None

    return {
        "id": str(row["id"]),
        "tender_number": row["tender_number"],
        "title": row["title"],
        "description": row["description"],
        "buyer": row["buyer_name"],
        "province": row["province"],
        "status": row["status"],
        "dates": {
            "published_at": _iso(row["published_at"]),
            "closing_at": _iso(row["closing_at"]),
            "closing_verified": closing_verified,
            "closing_source": closing_prov.get("source"),
            "closing_note": closing_note,
            "verify_at_source": None if closing_verified else row["original_url"],
            "briefing_at": _iso(row["briefing_at"]),
            "compulsory_briefing": row["compulsory_briefing"],
        },
        "value_estimated": (
            float(row["value_estimated"]) if row["value_estimated"] is not None else None
        ),
        "currency": row["currency"],
        "original_url": row["original_url"],
        "source_urls": row["source_urls"] or [],
    }


def _iso(dt) -> str | None:
    return dt.isoformat() if dt is not None else None
