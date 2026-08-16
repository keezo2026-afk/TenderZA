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
    parser.add_argument(
        # NOT a .local address: RFC 6762 reserves it, and the API's EmailStr
        # validator rejects reserved/special-use domains -- a demo admin that
        # cannot pass /auth/login's own validation is worse than none.
        "--demo-admin", default="admin@tenderza.example",
        help="email of the demo admin account created on first boot",
    )
    parser.add_argument(
        "--demo-password", default="tenderza-demo-admin",
        help="password for the demo admin (dev only — never reuse in prod)",
    )
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

    _seed_demo_admin(dsn, args.demo_admin, args.demo_password)

    print(f"[demo] starting API on 0.0.0.0:{args.port} ...")
    import uvicorn

    uvicorn.run("tenderza.api.app:app", host="0.0.0.0", port=args.port)
    return 0


def _seed_demo_admin(dsn: str, email: str, password: str) -> None:
    """Create the demo admin so /review and /ops are reachable (§17).

    The admin surfaces are role-gated, so a demo with no accounts would show
    nothing but sign-in forms. This seeds one known account rather than
    switching enforcement off, so what you click through locally is the same
    code path that runs in production.
    """
    import psycopg

    from tenderza.auth import hash_password

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT password_hash FROM users WHERE lower(email) = %s",
                    (email.lower(),))
        row = cur.fetchone()
        if row and row[0]:
            print(f"[demo] admin account {email} already present")
            return
        cur.execute(
            """
            INSERT INTO users (email, name, password_hash, role)
            VALUES (%s, 'Demo Admin', %s, 'admin')
            ON CONFLICT (email) DO UPDATE
                SET password_hash = EXCLUDED.password_hash,
                    role = EXCLUDED.role
            """,
            (email.lower(), hash_password(password)),
        )
        conn.commit()
    print(f"[demo] created admin account {email} / {password}")
    print("[demo] sign in at http://localhost:3000/review or /ops")


if __name__ == "__main__":
    raise SystemExit(main())
