#!/usr/bin/env python3
"""Document pipeline runner (Blueprint §6).

    DATABASE_URL=... python scripts/process_documents.py [--limit 50] \
        [--store-root ./data/objects]

Fetches unprocessed tender documents (content_hash IS NULL), stores them by
content hash, extracts text + fields, enriches tenders under the confidence
rule, and queues high-stakes low-confidence fields for review.
"""

from __future__ import annotations

import argparse
import os
import sys

import psycopg

from tenderza.documents.processor import process_pending
from tenderza.documents.storage import ObjectStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--store-root", default=os.environ.get(
        "OBJECT_STORE_ROOT", "./data/objects"))
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 1

    store = ObjectStore(args.store_root)
    with psycopg.connect(dsn) as conn:
        outcomes = process_pending(conn, store, limit=args.limit)

    for o in outcomes:
        if o.error:
            print(f"  [ERR ] doc {o.doc_id[:8]}: {o.error}")
        elif o.needs_ocr:
            print(f"  [OCR ] doc {o.doc_id[:8]}: scanned PDF — flagged for OCR stage")
        else:
            bits = [f"{o.fields_found} fields"]
            if o.enriched:
                bits.append(f"enriched: {', '.join(o.enriched)}")
            if o.queued_for_review:
                bits.append(f"review: {', '.join(o.queued_for_review)}")
            if o.deduped:
                bits.append("dup bytes")
            print(f"  [DONE] doc {o.doc_id[:8]}: {'; '.join(bits)}")

    print(f"processed {len(outcomes)} documents")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
