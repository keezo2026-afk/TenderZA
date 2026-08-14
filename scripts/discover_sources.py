#!/usr/bin/env python3
"""Discovery mode (Blueprint §5.2.1): fingerprint sources & update the registry.

    DATABASE_URL=... python scripts/discover_sources.py            # all DISCOVERY sources
    DATABASE_URL=... python scripts/discover_sources.py --all      # re-fingerprint everything
    python scripts/discover_sources.py --url https://x.gov.za/tenders   # one-off, no DB

Per source: probe the tender URL politely, classify platform, then update
sources.platform / crawl_method / adapter / adapter_config / status:
  workable fingerprint -> ACTIVE (generic adapter assigned)
  JS shell / bespoke   -> stays DISCOVERY (triage queue, §5.2.3)
  unreachable/404      -> DEGRADED with note (verify tender_url manually)
"""

from __future__ import annotations

import argparse
import os
import sys

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tenderza.crawl.fingerprint import classify, probe


def discover_one(url: str) -> None:
    ev = probe(url)
    fp = classify(ev)
    print(f"  platform={fp.platform} adapter={fp.adapter or '-'} "
          f"method={fp.crawl_method} conf={fp.confidence:.2f}")
    for note in fp.notes:
        print(f"    {note}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true",
                        help="re-fingerprint ACTIVE/DEGRADED sources too")
    parser.add_argument("--url", help="fingerprint a single URL (no DB)")
    args = parser.parse_args()

    if args.url:
        print(f"Probing {args.url}")
        discover_one(args.url)
        return 0

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 1

    statuses = ("DISCOVERY", "ACTIVE", "DEGRADED") if args.all else ("DISCOVERY",)
    with psycopg.connect(dsn) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, name, crawl_url, adapter FROM sources
                WHERE status = ANY(%s) AND crawl_url IS NOT NULL
                  AND crawl_method != 'API'      -- APIs don't need fingerprinting
                ORDER BY triage_tier NULLS LAST, name
                """,
                (list(statuses),),
            )
            sources = cur.fetchall()

        print(f"Fingerprinting {len(sources)} sources...\n")
        workable = blocked = unreachable = 0

        for src in sources:
            print(f"{src['name']}\n  {src['crawl_url']}")
            ev = probe(src["crawl_url"])
            fp = classify(ev)
            print(f"  -> platform={fp.platform} adapter={fp.adapter or '-'} "
                  f"conf={fp.confidence:.2f}  ({'; '.join(fp.notes)})\n")

            if fp.workable:
                workable += 1
                new_status, note = "ACTIVE", "; ".join(fp.notes)
            elif fp.platform == "UNKNOWN" and fp.crawl_method == "PLAYWRIGHT":
                blocked += 1
                new_status, note = "DISCOVERY", "; ".join(fp.notes)
            elif fp.platform in ("NONE", "UNKNOWN"):
                unreachable += 1
                new_status, note = "DEGRADED", "; ".join(fp.notes)
            else:
                new_status, note = "DISCOVERY", "; ".join(fp.notes)

            adapter_config = {}
            if ev.feed_url:
                adapter_config["feed_url"] = ev.feed_url

            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE sources SET
                        platform = %s::source_platform,
                        crawl_method = %s::crawl_method,
                        adapter = coalesce(%s, adapter),
                        adapter_config = adapter_config || %s,
                        status = %s::source_status,
                        note = %s,
                        last_checked = now()
                    WHERE id = %s
                    """,
                    (fp.platform, fp.crawl_method, fp.adapter,
                     Jsonb(adapter_config), new_status, note[:500], src["id"]),
                )
            conn.commit()

        print(f"Done: {workable} workable via generic adapters, "
              f"{blocked} WAF/JS (triage), {unreachable} unreachable/404, "
              f"{len(sources) - workable - blocked - unreachable} other")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
