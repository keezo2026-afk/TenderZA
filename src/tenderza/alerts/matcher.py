"""Alert matching (Blueprint §14) — saved-search criteria -> SQL.

Design rules enforced here:
* the engine reads ONLY the normalized tenders table (never crawl_results);
* a tender is notified AT MOST ONCE per alert (NOT EXISTS on alert_events);
* the closing-window filter applies ONLY to verified closing dates —
  an unverified date never drives a deadline-based alert (§10.3, §14).
  Keyword/region matches may still include unverified-date tenders (the
  email shows "verify at source" for them).
"""

from __future__ import annotations

from typing import Any

# Statuses eligible for new-match alerts. CLOSED/CANCELLED/AWARDED are
# never alert-worthy as "new opportunities".
ALERTABLE_STATUSES = ("OPEN", "CLOSING_SOON", "EXTENDED", "UNKNOWN", "NEW")

_SELECT = """
SELECT t.id, t.tender_number, t.title, t.description, t.province,
       t.status::text AS status, t.closing_at, t.compulsory_briefing,
       t.original_url, t.field_provenance, o.name AS buyer_name
FROM tenders t
LEFT JOIN organisations o ON o.id = t.buyer_id
"""


def build_match_query(alert: dict[str, Any], *, limit: int = 50) -> tuple[str, list]:
    """Return (sql, params) selecting unsent tenders matching this alert.

    ``alert`` is a user_alerts row: keywords (text), provinces (json list),
    categories (json list), batch_prefs (json — may hold closing_within_days),
    id (uuid).
    """
    where = [
        "t.status::text = ANY(%s)",
        # at-most-once per (alert, tender)
        """NOT EXISTS (
            SELECT 1 FROM alert_events e
            WHERE e.alert_id = %s AND e.tender_id = t.id
        )""",
    ]
    params: list[Any] = [list(ALERTABLE_STATUSES), alert["id"]]

    keywords = (alert.get("keywords") or "").strip()
    if keywords:
        where.append("t.search_vector @@ websearch_to_tsquery('english', %s)")
        params.append(keywords)

    provinces = list(alert.get("provinces") or [])
    if provinces:
        where.append("t.province = ANY(%s)")
        params.append(provinces)

    categories = list(alert.get("categories") or [])
    if categories:
        # categories is a jsonb array on tenders; overlap test
        where.append("t.categories ?| %s")
        params.append(categories)

    closing_days = (alert.get("batch_prefs") or {}).get("closing_within_days")
    if closing_days:
        # Deadline filters apply ONLY to verified closing dates (§14).
        where.append(
            """(
                (t.field_provenance->'closing_at'->>'confidence')::float >= 0.80
                AND t.closing_at BETWEEN now()
                    AND now() + make_interval(days => %s)
            )"""
        )
        params.append(int(closing_days))

    sql = (
        _SELECT
        + " WHERE "
        + " AND ".join(where)
        + " ORDER BY t.closing_at ASC NULLS LAST, t.created_at DESC LIMIT %s"
    )
    params.append(limit)
    return sql, params


def closing_verified(tender_row: dict[str, Any]) -> bool:
    prov = (tender_row.get("field_provenance") or {}).get("closing_at") or {}
    return (prov.get("confidence") or 0.0) >= 0.80
