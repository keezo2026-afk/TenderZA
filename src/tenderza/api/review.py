"""Review-queue endpoints (Blueprint §6) — the human-in-the-loop.

GET  /review                    open items + tender context + evidence
GET  /review/stats              queue depth by field
POST /review/{item_id}/resolve  approve | correct | reject

Resolution semantics (§6, §9):
* approve — the extracted value is confirmed: applied to the tender with
  provenance {source: SOURCE, source_id: human:<name>, confidence: 1.0}.
* correct — reviewer supplies the right value: applied the same way.
* reject  — extracted value is wrong and no correction is known: item
  closes, tender keeps its current value; provenance untouched.
Any applied change writes a tender_versions row (change alerts fire on
corrections, §9). Verified fields then unlock CLOSING_SOON/alerts because
confidence 1.0 clears every threshold.

Auth (§17): every endpoint requires the **analyst** role. The reviewer
identity is taken from the authenticated session, never from the request
body — otherwise anyone could stamp a human-verified provenance record with
someone else's name.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from tenderza import audit
from tenderza.auth import Principal, require_analyst

router = APIRouter(
    prefix="/review",
    tags=["review"],
    # Router-level: a new endpoint added here is protected by default rather
    # than by the author remembering to add a decorator.
    dependencies=[Depends(require_analyst)],
)

# Fields a reviewer may write through to the tenders table, and how to cast.
_FIELD_COLUMNS: dict[str, str] = {
    "closing_at": "closing_at",
    "briefing_at": "briefing_at",
    "compulsory_briefing": "compulsory_briefing",
    "value_estimated": "value_estimated",
    "tender_number": "tender_number",
    "bbee_level": None,          # lives inside requirements jsonb
    "cidb_grades": None,
}


class ResolveRequest(BaseModel):
    action: Literal["approve", "correct", "reject"]
    corrected_value: Any | None = None
    # NOTE: no `reviewer` field. Attribution comes from the session (§17);
    # accepting it from the body would let a caller sign a human-verified
    # provenance stamp as somebody else.


def get_pool():
    # Late import so this module stays importable without the app wiring.
    from tenderza.api.app import get_pool as _gp
    return _gp()


@router.get("")
def list_open_items(limit: int = 50, pool=Depends(get_pool)):
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT r.id, r.field, r.value, r.confidence, r.queued_at,
                   t.id AS tender_id, t.tender_number, t.title,
                   t.original_url, t.closing_at, t.status::text AS status,
                   o.name AS buyer_name
            FROM review_queue r
            JOIN tenders t ON t.id = r.tender_id
            LEFT JOIN organisations o ON o.id = t.buyer_id
            WHERE r.resolved_at IS NULL
            ORDER BY
                CASE r.field WHEN 'closing_at' THEN 0 ELSE 1 END,  -- deadlines first
                r.queued_at
            LIMIT %s
            """,
            (limit,),
        )
        items = cur.fetchall()

    return {
        "total": len(items),
        "items": [
            {
                "id": str(i["id"]),
                "field": i["field"],
                "extracted": i["value"],           # {value, confidence, evidence}
                "confidence": i["confidence"],
                "queued_at": i["queued_at"].isoformat(),
                "tender": {
                    "id": str(i["tender_id"]),
                    "tender_number": i["tender_number"],
                    "title": i["title"],
                    "buyer": i["buyer_name"],
                    "status": i["status"],
                    "current_closing_at": (
                        i["closing_at"].isoformat() if i["closing_at"] else None
                    ),
                    "verify_at_source": i["original_url"],
                },
            }
            for i in items
        ],
    }


@router.get("/stats")
def review_stats(pool=Depends(get_pool)):
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT field, count(*) FROM review_queue "
            "WHERE resolved_at IS NULL GROUP BY field ORDER BY 2 DESC"
        )
        open_by_field = dict(cur.fetchall())
        cur.execute(
            "SELECT count(*) FROM review_queue WHERE resolved_at IS NOT NULL"
        )
        resolved_total = cur.fetchone()[0]
    return {"open": sum(open_by_field.values()),
            "open_by_field": open_by_field,
            "resolved_total": resolved_total}


@router.post("/{item_id}/resolve")
def resolve_item(item_id: str, req: ResolveRequest,
                 principal: Principal = Depends(require_analyst),
                 pool=Depends(get_pool)):
    reviewer = principal.email
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            try:
                cur.execute(
                    "SELECT id, tender_id, field, value FROM review_queue "
                    "WHERE id = %s AND resolved_at IS NULL",
                    (item_id,),
                )
            except Exception as exc:  # invalid uuid text
                raise HTTPException(404, "review item not found") from exc
            item = cur.fetchone()
            if item is None:
                raise HTTPException(404, "review item not found (or already resolved)")

            applied = None
            if req.action in ("approve", "correct"):
                value = (
                    req.corrected_value
                    if req.action == "correct"
                    else (item["value"] or {}).get("value")
                )
                if value is None:
                    raise HTTPException(
                        422, "no value to apply (correct requires corrected_value)"
                    )
                applied = _apply_value(
                    cur, str(item["tender_id"]), item["field"], value, reviewer
                )

            # reviewed_by is a real FK, so it can only hold a principal that
            # has a users row. The service token and the auth-disabled dev
            # principal are invented in code, so for those we keep the
            # attribution in the resolution JSON and leave the FK null.
            reviewed_by = None if principal.is_synthetic else principal.user_id
            cur.execute(
                """
                UPDATE review_queue
                SET resolved_at = now(),
                    reviewed_by = %s,
                    resolution = %s
                WHERE id = %s
                """,
                (reviewed_by,
                 Jsonb({"action": req.action, "applied": applied,
                        "reviewer": reviewer}), item_id),
            )

        # Same transaction as the change itself: the tender edit and the
        # record of who made it commit together or not at all (§17).
        audit.record_for_principal(
            conn, principal, table_name="review_queue",
            action=audit.REVIEW_RESOLVED, record_id=item_id,
            detail={"decision": req.action, "applied": applied,
                    "field": item["field"],
                    "tender_id": str(item["tender_id"])},
        )
        conn.commit()

    return {"resolved": item_id, "action": req.action, "applied": applied}


def _apply_value(cur, tender_id: str, field: str, value: Any, reviewer: str):
    """Write the confirmed value + human provenance + version row."""
    provenance_patch = {
        field: {"source": "SOURCE", "source_id": f"human:{reviewer}",
                "confidence": 1.0}
    }

    column = _FIELD_COLUMNS.get(field)
    if column:
        # Read old value for the version diff.
        cur.execute(
            f"SELECT {column} FROM tenders WHERE id = %s", (tender_id,)  # noqa: S608
        )
        row = cur.fetchone()
        old_value = row[column] if row else None
        cur.execute(
            f"""
            UPDATE tenders
            SET {column} = %s,
                field_provenance = field_provenance || %s
            WHERE id = %s
            """,  # noqa: S608 — column from fixed whitelist
            (value, Jsonb(provenance_patch), tender_id),
        )
        changes = {field: {"old": str(old_value) if old_value is not None else None,
                           "new": str(value)}}
    else:
        # jsonb-resident fields (bbee_level, cidb_grades) -> requirements blob
        cur.execute(
            """
            UPDATE tenders
            SET requirements = requirements || %s,
                field_provenance = field_provenance || %s
            WHERE id = %s
            """,
            (Jsonb({field: value}), Jsonb(provenance_patch), tender_id),
        )
        changes = {field: {"old": None, "new": value}}

    cur.execute(
        """
        INSERT INTO tender_versions (tender_id, version_no, changes, change_kind)
        SELECT %s, coalesce(max(version_no), 0) + 1, %s, 'HUMAN_VERIFIED'
        FROM tender_versions WHERE tender_id = %s
        """,
        (tender_id, Jsonb(changes), tender_id),
    )

    if field == "closing_at":
        # A verified closing date unlocks real status (§10.3): recompute
        # OPEN / CLOSING_SOON / CLOSED from the now-trusted instant.
        cur.execute(
            """
            UPDATE tenders SET status = CASE
                WHEN closing_at <= now() THEN 'CLOSED'::tender_status
                WHEN closing_at <= now() + interval '72 hours'
                    THEN 'CLOSING_SOON'::tender_status
                ELSE 'OPEN'::tender_status
            END
            WHERE id = %s AND closing_at IS NOT NULL
            """,
            (tender_id,),
        )

    return {"field": field, "value": str(value)}
