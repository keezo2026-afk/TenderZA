"""Crawl scheduler (Blueprint §5.3) — DB-backed job runner v1.

State machine: PENDING -> FETCHING -> PARSING -> DONE | FAILED | RETRY_BACKOFF.

v1 is a synchronous poll-loop suitable for a single worker (P1/P2 scale:
tens of sources). The Celery+Redis deployment (§18) slots in later by
calling ``run_job`` from a Celery task — the state machine, backoff and
persistence semantics stay identical.

Behaviours per §5.3:
* due-source selection from `sources` (frequency_min vs last_checked);
* exponential backoff with attempt cap -> FAILED + source status DEGRADED/FAILED;
* append-only `crawl_results` row per run (raw notice payloads replayable);
* `source_health` row per run; last_success/last_checked maintained;
* adapter output flows through the SAME pipeline as OCDS ingest:
  normalize -> upsert (dedupe/versioning) — one write path, no side doors.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from tenderza.adapters.base import RawTenderNotice, SourceConfig, get_adapter
from tenderza.persistence import TenderStore
from tenderza.pipeline import normalize_notice

MAX_ATTEMPTS = 5
BASE_BACKOFF_MIN = 15


def backoff_minutes(attempt: int) -> int:
    """Exponential backoff: 15, 30, 60, 120, 240 min (capped)."""
    return BASE_BACKOFF_MIN * (2 ** min(attempt - 1, 4))


@dataclass
class JobOutcome:
    job_id: str
    source_name: str
    state: str                 # DONE | RETRY_BACKOFF | FAILED
    notices: int = 0
    created: int = 0
    updated: int = 0
    error: str | None = None


# ---------------------------------------------------------------------------
# Job creation
# ---------------------------------------------------------------------------

def enqueue_due_sources(conn: psycopg.Connection, *, limit: int = 50) -> int:
    """Create PENDING crawl_jobs for active sources whose frequency window
    has elapsed (or that were never crawled). Skips sources that already
    have a live job (PENDING/FETCHING/PARSING/RETRY_BACKOFF not yet due)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO crawl_jobs (source_id, state)
            SELECT s.id, 'PENDING'
            FROM sources s
            WHERE s.status IN ('ACTIVE', 'DEGRADED', 'DISCOVERY')
              AND s.adapter IS NOT NULL
              AND (
                    s.last_checked IS NULL
                 OR s.last_checked
                    < now() - make_interval(mins => coalesce(s.frequency_min, 1440))
              )
              AND NOT EXISTS (
                    SELECT 1 FROM crawl_jobs j
                    WHERE j.source_id = s.id
                      AND (
                            j.state IN ('PENDING', 'FETCHING', 'PARSING')
                         OR (j.state = 'RETRY_BACKOFF' AND j.next_retry_at > now())
                      )
              )
            LIMIT %s
            """,
            (limit,),
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# Job execution
# ---------------------------------------------------------------------------

def _source_config(row: dict[str, Any]) -> SourceConfig:
    options = dict(row.get("adapter_config") or {})
    options.setdefault("platform", row.get("platform"))
    return SourceConfig(
        source_id=str(row["source_id"]),
        name=row["name"],
        crawl_url=row.get("crawl_url") or "",
        adapter=row["adapter"],
        org_type=row.get("org_type") or "UNKNOWN",
        authority_score=row.get("authority_score") or 50,
        triage_tier=row.get("triage_tier"),
        options=options,
    )


def run_job(conn: psycopg.Connection, job_id: str) -> JobOutcome:
    """Execute one crawl job through the full state machine."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT j.id AS job_id, j.attempts, s.id AS source_id, s.name,
                   s.crawl_url, s.adapter, s.adapter_config, s.platform::text AS platform,
                   s.authority_score, s.triage_tier,
                   o.type::text AS org_type
            FROM crawl_jobs j
            JOIN sources s ON s.id = j.source_id
            LEFT JOIN organisations o ON o.id = s.org_id
            WHERE j.id = %s
            """,
            (job_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise KeyError(f"no such job {job_id}")

    attempts = (row["attempts"] or 0) + 1
    _set_job(conn, job_id, "FETCHING", attempts=attempts)

    config = _source_config(row)
    try:
        adapter = get_adapter(config)
        result = adapter.run()
    except Exception as exc:  # noqa: BLE001 — registry/config errors
        return _handle_failure(conn, row, attempts, f"{type(exc).__name__}: {exc}")

    if result.errors and not result.notices:
        return _handle_failure(conn, row, attempts, "; ".join(result.errors[:3]))

    _set_job(conn, job_id, "PARSING")

    store = TenderStore(conn)
    created = updated = 0
    for notice in result.notices:
        tender = normalize_notice(
            notice,
            authority_score=config.authority_score,
            structured=_is_structured(notice),
        )
        outcome = store.upsert_tender(tender)
        if outcome.created:
            created += 1
        elif outcome.changed:
            updated += 1

    _record_result(conn, job_id, result.notices, created, updated)
    _set_job(conn, job_id, "DONE")
    _touch_source(conn, str(row["source_id"]), ok=True,
                  note=f"{len(result.notices)} notices, {created} new, {updated} updated")
    conn.commit()

    return JobOutcome(str(job_id), row["name"], "DONE",
                      notices=len(result.notices), created=created, updated=updated)


def _is_structured(notice: RawTenderNotice) -> bool:
    """Feed/API items are structured; sitemap stubs and PDF bulletins are
    not — their fields must earn confidence via the document pipeline."""
    fmt = (notice.raw or {}).get("format", "")
    return fmt not in ("sitemap", "pdf_bulletin")


def _handle_failure(conn, row, attempts: int, error: str) -> JobOutcome:
    job_id, source_id = str(row["job_id"]), str(row["source_id"])
    if attempts >= MAX_ATTEMPTS:
        _set_job(conn, job_id, "FAILED", error=error)
        _touch_source(conn, source_id, ok=False, error=error, terminal=True)
        state = "FAILED"
    else:
        _set_job(conn, job_id, "RETRY_BACKOFF", error=error,
                 retry_in_min=backoff_minutes(attempts))
        _touch_source(conn, source_id, ok=False, error=error)
        state = "RETRY_BACKOFF"
    conn.commit()
    return JobOutcome(job_id, row["name"], state, error=error)


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _set_job(conn, job_id: str, state: str, *, attempts: int | None = None,
             error: str | None = None, retry_in_min: int | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE crawl_jobs SET
                state = %s::crawl_state,
                attempts = coalesce(%s, attempts),
                error = %s,
                next_retry_at = CASE WHEN %s::int IS NOT NULL
                    THEN now() + make_interval(mins => %s::int) END
            WHERE id = %s
            """,
            (state, attempts, error, retry_in_min, retry_in_min, job_id),
        )


def _record_result(conn, job_id: str, notices, created: int, updated: int) -> None:
    """Append-only crawl_results row (§5.3): raw notices replayable later."""
    raw = [
        {
            "source_url": n.source_url,
            "title": n.title,
            "tender_number": n.tender_number,
            "raw": n.raw,
        }
        for n in notices[:200]  # cap the blob; full docs live in object storage
    ]
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO crawl_results (job_id, raw, extracted_fields) "
            "VALUES (%s, %s, %s)",
            (job_id, Jsonb({"notices": raw}),
             Jsonb({"created": created, "updated": updated})),
        )


def _touch_source(conn, source_id: str, *, ok: bool, error: str | None = None,
                  note: str | None = None, terminal: bool = False,
                  status_code: int | None = None) -> None:
    with conn.cursor() as cur:
        if ok:
            cur.execute(
                """
                UPDATE sources SET last_checked = now(), last_success = now(),
                    status = CASE WHEN status = 'DEGRADED'
                                  THEN 'ACTIVE'::source_status ELSE status END,
                    note = coalesce(%s, note)
                WHERE id = %s
                """,
                (note, source_id),
            )
        else:
            cur.execute(
                """
                UPDATE sources SET last_checked = now(),
                    status = %s::source_status
                WHERE id = %s
                """,
                ("FAILED" if terminal else "DEGRADED", source_id),
            )
        # mttd_started_at stamps when the CURRENT breakage episode began (§16).
        # A failure carries forward the open episode's start if one exists, and
        # otherwise opens a new one at now(); a success closes the episode by
        # writing NULL. That makes MTTD/MTTR a subtraction rather than a
        # window-function reconstruction over the whole health log.
        cur.execute(
            """
            INSERT INTO source_health
                (source_id, checked_at, last_success, last_error, status_code,
                 mttd_started_at)
            SELECT %(sid)s,
                   clock_timestamp(),
                   CASE WHEN %(ok)s THEN clock_timestamp() END,
                   %(err)s,
                   %(code)s,
                   CASE WHEN %(ok)s THEN NULL ELSE coalesce(
                       (SELECT sh.mttd_started_at
                          FROM source_health sh
                         WHERE sh.source_id = %(sid)s
                         ORDER BY sh.checked_at DESC
                         LIMIT 1),
                       clock_timestamp()) END
            """,
            {"sid": source_id, "ok": ok, "err": error, "code": status_code},
        )


# ---------------------------------------------------------------------------
# Poll loop entry
# ---------------------------------------------------------------------------

def run_pending(conn: psycopg.Connection, *, limit: int = 20) -> list[JobOutcome]:
    """Run all runnable jobs (PENDING, or RETRY_BACKOFF whose time came)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM crawl_jobs
            WHERE state = 'PENDING'
               OR (state = 'RETRY_BACKOFF' AND next_retry_at <= now())
            ORDER BY run_time
            LIMIT %s
            """,
            (limit,),
        )
        job_ids = [str(r[0]) for r in cur.fetchall()]

    return [run_job(conn, job_id) for job_id in job_ids]
