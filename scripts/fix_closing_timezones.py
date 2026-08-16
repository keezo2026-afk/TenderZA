#!/usr/bin/env python3
"""One-time repair: re-derive eTenders instants that were read as UTC (P0).

Background
----------
Until 16 Aug 2026 the OCDS adapter parsed the portal's ``...T11:00:00Z``
timestamps as UTC. That portal actually emits **SAST wall-clock times with a
literal Z** (see ``tenderza.adapters.ocds_api`` for the evidence), so every
closing and briefing instant already stored is **two hours late**. A user
trusting a stored 13:00 SAST deadline would arrive to a closed bid box.

This script replays the verbatim archive in ``ocds_records`` — the reason we
keep it (§10.2.6) — and rewrites the affected columns in place.

Why not just re-run scripts/ingest_ocds.py?
-------------------------------------------
The normal upsert path would diff old-vs-new and classify a 2-hour move as
``SHORTENED``, i.e. "the buyer brought the deadline forward". That is false
and would pollute the change history that drives user notifications (§9).
This script writes a truthful ``TIMEZONE_CORRECTION`` version row instead,
and touches nothing else.

Usage
-----
    DATABASE_URL=postgresql://... python scripts/fix_closing_timezones.py --dry-run
    DATABASE_URL=postgresql://... python scripts/fix_closing_timezones.py --apply

Idempotent: a tender whose stored instants already match the re-derived ones
is skipped, so re-running changes nothing and logs no versions.
"""

from __future__ import annotations

import argparse
import os
import sys

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tenderza.adapters.ocds_api import release_to_notice
from tenderza.timeutil import SAST

SOURCE_ID = "etenders-ocds"
CHANGE_KIND = "TIMEZONE_CORRECTION"

# Columns this repair is allowed to touch, and the notice attribute each
# one is re-derived from. Nothing else is rewritten.
_FIELDS = {
    "closing_at": "closing_at",
    "briefing_at": "briefing_at",
    "published_at": "published_at",
}


def _fmt(dt) -> str:
    return "—" if dt is None else dt.astimezone(SAST).strftime("%Y-%m-%d %H:%M SAST")


def repair(conn: psycopg.Connection, *, apply: bool, limit: int | None,
           verbose: bool) -> dict[str, int]:
    stats = {"examined": 0, "repaired": 0, "unchanged": 0, "no_archive": 0}

    with conn.cursor(row_factory=dict_row) as cur:
        # Join tenders to their verbatim archived release. Only eTenders
        # rows have an ocid, so the join is the source filter.
        sql = """
            SELECT t.id, t.ocds_ocid, t.closing_at, t.briefing_at,
                   t.published_at, t.field_provenance,
                   r.raw
            FROM tenders t
            JOIN LATERAL (
                SELECT raw FROM ocds_records
                WHERE ocid = t.ocds_ocid
                ORDER BY fetched_at DESC NULLS LAST
                LIMIT 1
            ) r ON true
            WHERE t.ocds_ocid IS NOT NULL
            ORDER BY t.closing_at NULLS LAST
        """
        if limit:
            sql += f" LIMIT {int(limit)}"
        cur.execute(sql)
        rows = cur.fetchall()

    for row in rows:
        stats["examined"] += 1
        release = row["raw"]
        if not release:
            stats["no_archive"] += 1
            continue

        notice = release_to_notice(release, SOURCE_ID, "")
        changes: dict[str, dict[str, str | None]] = {}
        updates: dict[str, object] = {}

        for column, attr in _FIELDS.items():
            new_value = getattr(notice, attr)
            old_value = row[column]
            if new_value is None or old_value == new_value:
                continue
            changes[column] = {
                "old": old_value.isoformat() if old_value else None,
                "new": new_value.isoformat(),
            }
            updates[column] = new_value

        if not updates:
            stats["unchanged"] += 1
            continue

        # Provenance: the stored value no longer matches the published bytes.
        provenance_patch = {
            column: {
                "source": "DERIVED",
                "source_id": SOURCE_ID,
                "confidence": 0.98,
                "note": notice.derived_fields.get(column, "timezone corrected"),
            }
            for column in updates
            if column in notice.derived_fields
        }

        stats["repaired"] += 1
        if verbose:
            detail = ", ".join(
                f"{c}: {_fmt(row[c])} -> {_fmt(updates[c])}" for c in updates
            )
            print(f"  {row['ocds_ocid']}  {detail}", flush=True)

        if not apply:
            continue

        assignments = ", ".join(f"{c} = %({c})s" for c in updates)
        params = {**updates, "id": row["id"], "prov": Jsonb(provenance_patch)}
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""
                UPDATE tenders
                SET {assignments},
                    field_provenance = field_provenance || %(prov)s,
                    updated_at = now()
                WHERE id = %(id)s
                """,  # noqa: S608 — column names come from the fixed _FIELDS map
                params,
            )
            cur.execute(
                """
                INSERT INTO tender_versions (tender_id, version_no, changes, change_kind)
                SELECT %s, coalesce(max(version_no), 0) + 1, %s, %s
                FROM tender_versions WHERE tender_id = %s
                """,
                (row["id"], Jsonb(changes), CHANGE_KIND, row["id"]),
            )

    # Deliberately no commit here: transaction control belongs to the caller
    # (main() commits; tests roll back).
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--apply", action="store_true", help="write the repair")
    group.add_argument("--dry-run", action="store_true",
                       help="report what would change, touch nothing")
    parser.add_argument("--limit", type=int, help="cap rows examined (testing)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2

    with psycopg.connect(dsn) as conn:
        stats = repair(conn, apply=args.apply, limit=args.limit,
                       verbose=not args.quiet)
        if args.apply:
            conn.commit()
        else:
            conn.rollback()

    mode = "APPLIED" if args.apply else "DRY RUN (nothing written)"
    print(f"\n{mode}: {stats}")
    if not args.apply and stats["repaired"]:
        print("Re-run with --apply to write these corrections.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
