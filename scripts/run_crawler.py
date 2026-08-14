#!/usr/bin/env python3
"""Crawl scheduler loop (Blueprint §5.3).

    DATABASE_URL=... python scripts/run_crawler.py           # one pass
    DATABASE_URL=... python scripts/run_crawler.py --loop 300 # poll every 300s

Each pass: enqueue due sources -> run pending jobs (incl. matured
RETRY_BACKOFF) -> print outcomes. Single-worker v1; the Celery deployment
(§18) reuses run_job unchanged.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import psycopg

from tenderza.crawl.scheduler import enqueue_due_sources, run_pending


def one_pass(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        enqueued = enqueue_due_sources(conn)
        conn.commit()
        outcomes = run_pending(conn)

    print(f"enqueued={enqueued} ran={len(outcomes)}")
    for o in outcomes:
        line = f"  [{o.state}] {o.source_name}"
        if o.state == "DONE":
            line += f" — {o.notices} notices, {o.created} new, {o.updated} updated"
        else:
            line += f" — {o.error}"
        print(line)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", type=int, metavar="SECONDS",
                        help="poll continuously at this interval")
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 1

    if args.loop:
        print(f"crawler loop: every {args.loop}s (Ctrl-C to stop)")
        while True:
            one_pass(dsn)
            time.sleep(args.loop)
    else:
        one_pass(dsn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
