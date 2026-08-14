#!/usr/bin/env python3
"""Seed the Source Registry into Postgres (Blueprint §4, Phase 0).

Usage:
    DATABASE_URL=postgresql://tenderza:tenderza@localhost:5432/tenderza \
        python scripts/seed_registry.py

Idempotent: upserts on source name. Creates the owning organisation row
when one does not exist yet.
"""

from __future__ import annotations

import os
import sys

import psycopg

from tenderza.registry import load_seed_sources

ORG_TYPE_MAP = {
    "METRO": "METRO",
    "DISTRICT": "DISTRICT",
    "LOCAL": "LOCAL",
    "PROVINCE": "PROVINCE",
    "NATIONAL": "NATIONAL",
    "SOE": "SOE",
    "PUBLIC_ENTITY": "PUBLIC_ENTITY",
    "UNIVERSITY_TVET": "UNIVERSITY_TVET",
    "AGGREGATOR": "AGGREGATOR",
}


def main() -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 1

    rows = load_seed_sources()
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for row in rows:
            cur.execute(
                """
                INSERT INTO organisations (name, type, province, municipality, website)
                VALUES (%s, %s::org_type, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (
                    row["name"],
                    ORG_TYPE_MAP[row["type"]],
                    row.get("province"),
                    row.get("municipality"),
                    row.get("website"),
                ),
            )
            org = cur.fetchone()
            if org is None:
                cur.execute("SELECT id FROM organisations WHERE name = %s", (row["name"],))
                org = cur.fetchone()

            cur.execute(
                """
                INSERT INTO sources (
                    org_id, name, crawl_url, platform, crawl_method, adapter,
                    triage_tier, authority_score, frequency_min, status, note
                )
                VALUES (%s, %s, %s, %s::source_platform, %s::crawl_method, %s,
                        %s, %s, %s, %s::source_status, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    org[0],
                    row["name"],
                    row.get("tender_url"),
                    row.get("platform", "UNKNOWN"),
                    row.get("crawl_method", "NONE"),
                    row.get("adapter"),
                    row.get("triage_tier"),
                    row.get("authority_score", 50),
                    row.get("frequency_min"),
                    row.get("status", "DISCOVERY"),
                    row.get("note") or None,
                ),
            )
        conn.commit()

    print(f"Seeded {len(rows)} sources.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
