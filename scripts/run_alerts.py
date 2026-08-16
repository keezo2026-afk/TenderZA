#!/usr/bin/env python3
"""Alert engine runner (Blueprint §14) — cron entry point.

    DATABASE_URL=... python scripts/run_alerts.py
    DATABASE_URL=... PUBLIC_BASE_URL=https://tenderza.example \
        SMTP_HOST=smtp.sendgrid.net SMTP_USER=apikey SMTP_PASSWORD=... \
        python scripts/run_alerts.py

Without SMTP_HOST, digests land in the dev outbox (ALERT_OUTBOX,
default ./data/outbox) as .eml files.
"""

from __future__ import annotations

import os
import sys

import psycopg

from tenderza.alerts.engine import run_alerts


def main() -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 1

    with psycopg.connect(dsn) as conn:
        summary = run_alerts(conn, base_url=os.environ.get("PUBLIC_BASE_URL", ""))

    print(f"alerts checked: {summary.alerts_checked}; "
          f"emails sent: {summary.emails_sent}; "
          f"tenders notified: {summary.tenders_notified}")
    for r in summary.runs:
        if r.error:
            print(f"  [ERR ] alert {r.alert_id[:8]}: {r.error}")
        elif r.sent:
            print(f"  [SENT] alert {r.alert_id[:8]} -> {r.email}: "
                  f"{r.matched} tenders ({r.delivery_ref})")
        elif r.matched:
            print(f"  [SKIP] alert {r.alert_id[:8]}: {r.matched} matches, "
                  "no email channel")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
