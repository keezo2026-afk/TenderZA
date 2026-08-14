#!/usr/bin/env python3
"""Ingest eTender OCDS releases into Postgres (Blueprint §3.1, §5.1, §10.2.6).

Live poll (last N days):
    DATABASE_URL=postgresql://tenderza:tenderza@localhost:5432/tenderza \
        python scripts/ingest_ocds.py --days 7

Historical backfill (the ~158k-release archive since May 2021 — run in
month-sized windows so a failure loses at most one month):
    python scripts/ingest_ocds.py --backfill 2021-05

Explicit window / test pull:
    python scripts/ingest_ocds.py --date-from 2026-08-01 --date-to 2026-08-14 \
        --max-pages 2 --page-size 100

Flow per release: archive raw into ocds_records (verbatim, §10.2.6)
-> release_to_notice -> normalize_notice (provenance/confidence, §10)
-> TenderStore.upsert_tender (dedupe by fingerprint, version diffs, §7/§9).
Commits per page so interrupted runs resume cheaply (re-ingesting a page is
idempotent: ocds_records conflict-ignores, tenders diff to no-op).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta, timezone

import psycopg

from tenderza.adapters.base import SourceConfig
from tenderza.adapters.ocds_api import OcdsApiAdapter
from tenderza.persistence import TenderStore
from tenderza.pipeline import normalize_notice

ETENDERS_AUTHORITY = 100  # §8: official national portal
SOURCE_ID = "etenders-ocds"


def month_windows(start: str, end: date | None = None):
    """Yield (date_from, date_to) month windows from YYYY-MM to today."""
    year, month = (int(p) for p in start.split("-"))
    end = end or date.today()
    current = date(year, month, 1)
    while current <= end:
        nxt = date(current.year + (current.month == 12), (current.month % 12) + 1, 1)
        yield current.isoformat(), min(nxt - timedelta(days=1), end).isoformat()
        current = nxt


def ingest_window(
    conn: psycopg.Connection,
    date_from: str,
    date_to: str,
    *,
    page_size: int,
    max_pages: int,
    quiet: bool = False,
) -> dict[str, int]:
    config = SourceConfig(
        source_id=SOURCE_ID,
        name="National Treasury eTender Transparency Portal (OCDS)",
        crawl_url="https://ocds-api.etenders.gov.za",
        adapter="ocds_api",
        authority_score=ETENDERS_AUTHORITY,
        options={
            "date_from": date_from,
            "date_to": date_to,
            "page_size": page_size,
            "max_pages": max_pages,
        },
    )
    adapter = OcdsApiAdapter(config)
    store = TenderStore(conn)

    stats = {"releases": 0, "archived": 0, "created": 0, "updated": 0, "unchanged": 0}
    batch = 0
    for notice in adapter.fetch():
        stats["releases"] += 1
        if store.archive_ocds_release(notice.raw):
            stats["archived"] += 1

        tender = normalize_notice(notice, authority_score=ETENDERS_AUTHORITY)
        result = store.upsert_tender(tender)
        if result.created:
            stats["created"] += 1
        elif result.changed:
            stats["updated"] += 1
        else:
            stats["unchanged"] += 1

        batch += 1
        if batch >= page_size:  # commit roughly per page
            conn.commit()
            batch = 0
            if not quiet:
                print(f"  ... {stats['releases']} releases processed", flush=True)

    conn.commit()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, help="poll the last N days")
    parser.add_argument("--backfill", metavar="YYYY-MM",
                        help="backfill month-by-month from this month to today")
    parser.add_argument("--date-from")
    parser.add_argument("--date-to")
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--max-pages", type=int, default=0, help="0 = unlimited")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 1

    today = datetime.now(timezone.utc).date()
    windows: list[tuple[str, str]]
    if args.backfill:
        windows = list(month_windows(args.backfill))
    elif args.days:
        windows = [((today - timedelta(days=args.days)).isoformat(), today.isoformat())]
    elif args.date_from:
        windows = [(args.date_from, args.date_to or today.isoformat())]
    else:
        parser.error("one of --days / --backfill / --date-from is required")
        return 2

    totals = {"releases": 0, "archived": 0, "created": 0, "updated": 0, "unchanged": 0}
    with psycopg.connect(dsn) as conn:
        for date_from, date_to in windows:
            if not args.quiet:
                print(f"Window {date_from} .. {date_to}", flush=True)
            stats = ingest_window(
                conn, date_from, date_to,
                page_size=args.page_size, max_pages=args.max_pages,
                quiet=args.quiet,
            )
            for key in totals:
                totals[key] += stats[key]
            if not args.quiet:
                print(f"  {stats}", flush=True)

        store = TenderStore(conn)
        print(f"\nTotals: {totals}")
        print(f"DB counts: {store.counts()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
