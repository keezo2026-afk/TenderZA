"""Alert engine (Blueprint §14): match -> batch -> send -> record.

run_alerts() walks active user_alerts, finds unsent matching tenders
(matcher.py), renders ONE digest email per alert per run (batching — no
per-tender spam), sends it, and records every (alert, tender) pair in
alert_events so nothing is ever sent twice. Alert precision KPI (§16)
reads from alert_events (sent vs clicked).

Failure isolation: one alert's failure never blocks others; events are
committed per alert AFTER a successful send.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import psycopg
from psycopg.rows import dict_row

from tenderza.alerts.emailer import render_digest, send_email
from tenderza.alerts.matcher import build_match_query


@dataclass
class AlertRun:
    alert_id: str
    email: str | None
    matched: int = 0
    sent: bool = False
    delivery_ref: str | None = None
    error: str | None = None


@dataclass
class RunSummary:
    alerts_checked: int = 0
    emails_sent: int = 0
    tenders_notified: int = 0
    runs: list[AlertRun] = field(default_factory=list)


def run_alerts(conn: psycopg.Connection, *, base_url: str = "",
               max_per_digest: int = 25) -> RunSummary:
    summary = RunSummary()

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT a.id, a.keywords, a.provinces, a.categories,
                   a.batch_prefs, a.channels, u.email, u.name AS user_name
            FROM user_alerts a
            JOIN users u ON u.id = a.user_id
            WHERE a.active
            ORDER BY a.id
            """
        )
        alerts = cur.fetchall()

    summary.alerts_checked = len(alerts)

    for alert in alerts:
        run = AlertRun(alert_id=str(alert["id"]), email=alert["email"])
        summary.runs.append(run)
        try:
            sql, params = build_match_query(alert, limit=max_per_digest)
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                tenders = cur.fetchall()
            run.matched = len(tenders)
            if not tenders:
                continue

            channels = list(alert.get("channels") or ["email"])
            if "email" in channels and alert["email"]:
                subject, body = render_digest(
                    {**alert, "user_name": alert.get("user_name")},
                    tenders, base_url=base_url,
                )
                run.delivery_ref = send_email(alert["email"], subject, body)
                run.sent = True
                summary.emails_sent += 1

            # Record events AFTER successful delivery (or when no email
            # channel is configured, record anyway so matches don't pile up).
            with conn.cursor() as cur:
                for t in tenders:
                    cur.execute(
                        """
                        INSERT INTO alert_events (alert_id, tender_id, channel)
                        SELECT %s, %s, 'email'
                        WHERE NOT EXISTS (
                            SELECT 1 FROM alert_events
                            WHERE alert_id = %s AND tender_id = %s
                        )
                        """,
                        (alert["id"], t["id"], alert["id"], t["id"]),
                    )
            conn.commit()
            summary.tenders_notified += run.matched
        except Exception as exc:  # noqa: BLE001 — isolate per alert
            conn.rollback()
            run.error = f"{type(exc).__name__}: {exc}"

    return summary
