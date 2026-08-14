#!/usr/bin/env python3
"""One-command demo: embedded Postgres + schema + seed + LIVE OCDS pull + API.

    pip install -e ".[dev]" && python scripts/dev_demo.py

Boots pgserver (pip-installed PostgreSQL 16 with pgvector — no Docker
needed), applies db/schema.sql, seeds the registry, ingests the last
--days N (default 3) of real eTender OCDS releases, then serves the
FastAPI app on --port (default 8000).

Dev convenience only — production uses a managed Postgres (§18).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=3, help="OCDS lookback window")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--pgdata", default="/tmp/tenderza-pgdata")
    parser.add_argument("--skip-ingest", action="store_true")
    args = parser.parse_args()

    import pgserver

    print(f"[demo] starting embedded Postgres in {args.pgdata} ...")
    db = pgserver.get_server(args.pgdata)
    dsn = db.get_uri()
    os.environ["DATABASE_URL"] = dsn

    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        applied = conn.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='tenders'"
        ).fetchone()[0]
        if not applied:
            print("[demo] applying db/schema.sql ...")
            conn.execute((ROOT / "db" / "schema.sql").read_text())
        else:
            print("[demo] schema already present")

    print("[demo] seeding source registry ...")
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "seed_registry.py")],
        check=True, env=os.environ,
    )

    if not args.skip_ingest:
        print(f"[demo] ingesting LIVE OCDS releases (last {args.days} days) ...")
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "ingest_ocds.py"),
             "--days", str(args.days), "--page-size", "200"],
            env=os.environ, capture_output=True, text=True,
        )
        if result.returncode != 0:
            print("[demo] WARNING: live ingest failed (network?); trying "
                  "bundled sample data instead")
            samples = sorted((ROOT / "data" / "samples").glob("*.json"))
            if samples:
                subprocess.run(
                    [sys.executable, str(ROOT / "scripts" / "ingest_ocds.py"),
                     "--from-file", *[str(p) for p in samples]],
                    env=os.environ,
                )
            else:
                print("[demo] no sample data found; API will serve an empty table")
        else:
            print(result.stdout[-500:])

    print(f"[demo] starting API on 0.0.0.0:{args.port} ...")
    import uvicorn

    uvicorn.run("tenderza.api.app:app", host="0.0.0.0", port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
