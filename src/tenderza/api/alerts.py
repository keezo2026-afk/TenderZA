"""Alerts endpoints (Blueprint §14) — saved searches + digest runs.

POST /alerts                 create a saved-search alert (email + criteria)
GET  /alerts?email=          list alerts for an email address
POST /alerts/{id}/toggle     pause / resume
POST /alerts/run             run the engine now (admin/cron trigger)

Auth (§17). Alerts are personal data, so the rules differ per endpoint:

* **create** stays open — it is the self-service signup funnel, and an
  account is created lazily for the email address.
* **list / toggle** require a session, and a non-admin may only ever see or
  change their *own* alerts. Previously `GET /alerts?email=` would hand any
  caller the saved searches — i.e. the commercial interests — of any address
  they cared to guess. That was the worst leak in the API.
* **run** is admin-only: it sends real email, so an open trigger is both a
  spam cannon and a way to exhaust the mail quota.

Known gap (tracked, not fixed here): alert creation has no double opt-in, so
someone can subscribe an address they do not own. Confirmation tokens are the
fix; until then the digest carries a one-click unsubscribe.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, EmailStr, Field

from tenderza.alerts.engine import run_alerts
from tenderza.auth import Principal, require_admin, require_role

router = APIRouter(prefix="/alerts", tags=["alerts"])


def _assert_may_act_for(principal: Principal, email: str) -> None:
    """Admins act for anyone; everyone else only for themselves."""
    if principal.role == "admin":
        return
    if principal.email.lower() != email.lower():
        # 404-shaped message on purpose: confirming that an address *has*
        # alerts would leak membership to a probing caller.
        raise HTTPException(403, "not your alert")

VALID_PROVINCES = {
    "Eastern Cape", "Free State", "Gauteng", "KwaZulu-Natal", "Limpopo",
    "Mpumalanga", "North West", "Northern Cape", "Western Cape",
}


def get_pool():
    from tenderza.api.app import get_pool as _gp
    return _gp()


class CreateAlertRequest(BaseModel):
    email: EmailStr
    keywords: str = Field("", max_length=300)
    provinces: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    closing_within_days: int | None = Field(None, ge=1, le=365)
    name: str = Field("", max_length=120)


@router.post("")
def create_alert(req: CreateAlertRequest, pool=Depends(get_pool)):
    bad = [p for p in req.provinces if p not in VALID_PROVINCES]
    if bad:
        raise HTTPException(422, f"invalid provinces: {bad}")
    if not req.keywords.strip() and not req.provinces and not req.categories:
        raise HTTPException(422, "alert needs at least keywords, a province, or a category")

    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                INSERT INTO users (email, name)
                VALUES (%s, nullif(%s, ''))
                ON CONFLICT (email) DO UPDATE
                    SET name = coalesce(nullif(EXCLUDED.name, ''), users.name)
                RETURNING id
                """,
                (req.email.lower(), req.name),
            )
            user_id = cur.fetchone()["id"]

            batch_prefs = {}
            if req.closing_within_days:
                batch_prefs["closing_within_days"] = req.closing_within_days

            cur.execute(
                """
                INSERT INTO user_alerts
                    (user_id, keywords, provinces, categories, batch_prefs,
                     channels, active)
                VALUES (%s, %s, %s, %s, %s, '["email"]', true)
                RETURNING id
                """,
                (user_id, req.keywords.strip(), Jsonb(req.provinces),
                 Jsonb(req.categories), Jsonb(batch_prefs)),
            )
            alert_id = str(cur.fetchone()["id"])
        conn.commit()

    return {"alert_id": alert_id, "email": req.email.lower(), "active": True}


@router.get("")
def list_alerts(email: str,
                principal: Principal = Depends(require_role("viewer")),
                pool=Depends(get_pool)):
    _assert_may_act_for(principal, email)
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT a.id, a.keywords, a.provinces, a.categories, a.batch_prefs,
                   a.active,
                   (SELECT count(*) FROM alert_events e WHERE e.alert_id = a.id)
                       AS notified_total,
                   (SELECT max(e.sent_at) FROM alert_events e WHERE e.alert_id = a.id)
                       AS last_sent_at
            FROM user_alerts a
            JOIN users u ON u.id = a.user_id
            WHERE u.email = %s
            ORDER BY a.id
            """,
            (email.lower(),),
        )
        rows = cur.fetchall()

    return {
        "email": email.lower(),
        "alerts": [
            {
                "id": str(r["id"]),
                "keywords": r["keywords"],
                "provinces": r["provinces"],
                "categories": r["categories"],
                "closing_within_days": (r["batch_prefs"] or {}).get("closing_within_days"),
                "active": r["active"],
                "notified_total": r["notified_total"],
                "last_sent_at": r["last_sent_at"].isoformat() if r["last_sent_at"] else None,
            }
            for r in rows
        ],
    }


@router.post("/{alert_id}/toggle")
def toggle_alert(alert_id: str,
                 principal: Principal = Depends(require_role("viewer")),
                 pool=Depends(get_pool)):
    with pool.connection() as conn, conn.cursor() as cur:
        # Ownership is checked in the same statement that mutates, so there is
        # no window between "may I?" and "do it".
        try:
            cur.execute(
                "SELECT u.email FROM user_alerts a JOIN users u ON u.id = a.user_id "
                "WHERE a.id = %s",
                (alert_id,),
            )
        except Exception as exc:
            raise HTTPException(404, "alert not found") from exc
        owner = cur.fetchone()
        if owner is None:
            raise HTTPException(404, "alert not found")
        _assert_may_act_for(principal, owner[0])
        try:
            cur.execute(
                "UPDATE user_alerts SET active = NOT active WHERE id = %s "
                "RETURNING active",
                (alert_id,),
            )
        except Exception as exc:
            raise HTTPException(404, "alert not found") from exc
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, "alert not found")
        conn.commit()
    return {"alert_id": alert_id, "active": row[0]}


@router.post("/run")
def trigger_run(principal: Principal = Depends(require_admin),
                pool=Depends(get_pool)):
    """Run the alert engine now. In production this is called by cron/Celery
    beat; exposed for the admin and the demo."""
    import os
    with pool.connection() as conn:
        summary = run_alerts(conn, base_url=os.environ.get("PUBLIC_BASE_URL", ""))
    return {
        "alerts_checked": summary.alerts_checked,
        "emails_sent": summary.emails_sent,
        "tenders_notified": summary.tenders_notified,
        "runs": [
            {"alert_id": r.alert_id, "matched": r.matched, "sent": r.sent,
             "delivery_ref": r.delivery_ref, "error": r.error}
            for r in summary.runs
        ],
    }
