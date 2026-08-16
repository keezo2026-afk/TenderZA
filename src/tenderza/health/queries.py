"""Health/ops reads over sources, crawl_jobs, crawl_results, source_health.

Read-only. Everything returns plain dicts so the API layer, the pager
script and the tests share exactly one data shape (Blueprint §15, §16).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

from tenderza.health.metrics import HealthVerdict, classify_source

# One row per source, joined with its latest health signals.
#
# consecutive_failures counts health rows since the most recent success —
# i.e. the length of the currently-open breakage episode; breakage_started_at
# is when that episode began (the MTTD clock start, §16).
_SOURCE_HEALTH_SQL = """
WITH last_success AS (
    SELECT source_id, max(checked_at) AS at
    FROM source_health
    WHERE last_success IS NOT NULL
    GROUP BY source_id
),
episode AS (
    SELECT sh.source_id,
           count(*)          AS consecutive_failures,
           -- Prefer the episode start the scheduler stamped; fall back to the
           -- first failing check for rows written before mttd_started_at existed.
           coalesce(min(sh.mttd_started_at), min(sh.checked_at))
                             AS breakage_started_at,
           max(sh.checked_at) AS last_failure_at
    FROM source_health sh
    LEFT JOIN last_success ls ON ls.source_id = sh.source_id
    WHERE sh.last_success IS NULL
      AND (ls.at IS NULL OR sh.checked_at > ls.at)
    GROUP BY sh.source_id
),
latest_err AS (
    SELECT DISTINCT ON (source_id) source_id, last_error, status_code, checked_at
    FROM source_health
    WHERE last_error IS NOT NULL
    ORDER BY source_id, checked_at DESC
),
live_job AS (
    SELECT DISTINCT ON (source_id) source_id, state::text AS open_job_state,
           attempts, next_retry_at, run_time
    FROM crawl_jobs
    WHERE state IN ('PENDING', 'FETCHING', 'PARSING', 'RETRY_BACKOFF')
    ORDER BY source_id, run_time DESC
),
last_run AS (
    SELECT DISTINCT ON (cj.source_id)
           cj.source_id,
           cj.state::text AS last_job_state,
           cj.run_time,
           coalesce((cr.extracted_fields ->> 'created')::int, 0)
             + coalesce((cr.extracted_fields ->> 'updated')::int, 0) AS notices_last_run
    FROM crawl_jobs cj
    LEFT JOIN crawl_results cr ON cr.job_id = cj.id
    WHERE cj.state IN ('DONE', 'FAILED')
    ORDER BY cj.source_id, cj.run_time DESC
)
SELECT s.id, s.name, s.status::text AS status, s.triage_tier, s.frequency_min,
       s.platform::text AS platform, s.adapter, s.crawl_url,
       s.authority_score, s.last_checked, s.last_success,
       o.province,
       coalesce(e.consecutive_failures, 0) AS consecutive_failures,
       e.breakage_started_at,
       le.last_error, le.status_code,
       lj.open_job_state, lj.next_retry_at,
       lr.last_job_state, lr.notices_last_run
FROM sources s
LEFT JOIN organisations o ON o.id = s.org_id
LEFT JOIN episode e   ON e.source_id = s.id
LEFT JOIN latest_err le ON le.source_id = s.id
LEFT JOIN live_job lj ON lj.source_id = s.id
LEFT JOIN last_run lr ON lr.source_id = s.id
ORDER BY s.triage_tier NULLS LAST, s.name
"""


def fetch_source_rows(conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SOURCE_HEALTH_SQL)
        return [dict(r) for r in cur.fetchall()]


def source_verdicts(conn: psycopg.Connection, *,
                    now: datetime | None = None) -> list[HealthVerdict]:
    """Classified health for every registered source (§15)."""
    now = now or datetime.now(timezone.utc)
    return [classify_source(row, now=now) for row in fetch_source_rows(conn)]


def crawl_stats(conn: psycopg.Connection, *, hours: int = 24) -> dict[str, Any]:
    """Crawl activity over the last N hours (§15 'Crawl stats (24h)')."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT state::text AS state, count(*) AS n
            FROM crawl_jobs
            WHERE run_time > now() - make_interval(hours => %s)
            GROUP BY 1
            """,
            (hours,),
        )
        by_state = {r["state"]: r["n"] for r in cur.fetchall()}

        cur.execute(
            """
            SELECT coalesce(sum((extracted_fields ->> 'created')::int), 0) AS created,
                   coalesce(sum((extracted_fields ->> 'updated')::int), 0) AS updated,
                   count(*) AS results
            FROM crawl_results
            WHERE fetched_at > now() - make_interval(hours => %s)
            """,
            (hours,),
        )
        vol = dict(cur.fetchone())

        cur.execute(
            """
            SELECT count(*) AS docs
            FROM tender_documents
            WHERE content_hash IS NOT NULL
            """
        )
        docs = cur.fetchone()["docs"]

        cur.execute(
            "SELECT count(*) AS open FROM review_queue WHERE resolved_at IS NULL"
        )
        review_open = cur.fetchone()["open"]

    total = sum(by_state.values())
    done = by_state.get("DONE", 0)
    return {
        "window_hours": hours,
        "jobs_total": total,
        "jobs_by_state": by_state,
        # §16 "≥99% job success" — reported honestly, never rounded up.
        "job_success_pct": round(100.0 * done / total, 1) if total else None,
        "tenders_created": vol["created"],
        "tenders_updated": vol["updated"],
        "crawl_results": vol["results"],
        "documents_stored": docs,
        "review_queue_open": review_open,
    }


def freshness_by_tier(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Per-tier freshness against the §16 SLA table."""
    from tenderza.health.metrics import TIER_SLA_MINUTES

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT triage_tier,
                   count(*) AS sources,
                   count(*) FILTER (WHERE last_success IS NOT NULL) AS ever_succeeded,
                   avg(extract(epoch FROM (now() - last_success)) / 60.0)
                     FILTER (WHERE last_success IS NOT NULL) AS avg_age_min,
                   max(extract(epoch FROM (now() - last_success)) / 60.0)
                     FILTER (WHERE last_success IS NOT NULL) AS max_age_min
            FROM sources
            WHERE status NOT IN ('PUBLISH_NOTHING', 'INACTIVE')
            GROUP BY triage_tier
            ORDER BY triage_tier NULLS LAST
            """
        )
        rows = [dict(r) for r in cur.fetchall()]

    for r in rows:
        sla = TIER_SLA_MINUTES.get(r["triage_tier"] or 0, 1440)
        r["sla_minutes"] = sla
        r["avg_age_min"] = round(r["avg_age_min"], 1) if r["avg_age_min"] else None
        r["max_age_min"] = round(r["max_age_min"], 1) if r["max_age_min"] else None
        r["within_sla"] = (r["max_age_min"] is not None and r["max_age_min"] <= sla)
    return rows


def adapter_health(conn: psycopg.Connection, *, days: int = 7) -> list[dict[str, Any]]:
    """Per-adapter success trend — the §15 'adapter rot' signal.

    A site redesign shows up here as a parse-failure/zero-yield trend
    before it silently kills a source.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT coalesce(s.adapter, '(none)') AS adapter,
                   count(DISTINCT s.id) AS sources,
                   count(cj.id) AS runs,
                   count(cj.id) FILTER (WHERE cj.state = 'DONE') AS ok,
                   count(cj.id) FILTER (WHERE cj.state = 'FAILED') AS failed,
                   coalesce(sum((cr.extracted_fields ->> 'created')::int), 0) AS created
            FROM sources s
            LEFT JOIN crawl_jobs cj
                   ON cj.source_id = s.id
                  AND cj.run_time > now() - make_interval(days => %s)
            LEFT JOIN crawl_results cr ON cr.job_id = cj.id
            GROUP BY 1
            ORDER BY 1
            """,
            (days,),
        )
        rows = [dict(r) for r in cur.fetchall()]

    for r in rows:
        r["window_days"] = days
        r["success_pct"] = (
            round(100.0 * r["ok"] / r["runs"], 1) if r["runs"] else None
        )
        # Runs that "succeed" but never yield a tender are the classic
        # silent-redesign signature (§19 adapter rot).
        r["silent"] = bool(r["ok"] and r["created"] == 0)
    return rows


def recent_failures(conn: psycopg.Connection, *, limit: int = 20) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT s.name AS source_name, s.id AS source_id, s.triage_tier,
                   sh.checked_at, sh.last_error, sh.status_code
            FROM source_health sh
            JOIN sources s ON s.id = sh.source_id
            WHERE sh.last_error IS NOT NULL
            ORDER BY sh.checked_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]


def mttd_mttr_report(conn: psycopg.Connection, *, days: int = 30) -> dict[str, Any]:
    """Closed-episode MTTD/MTTR over the window (§16).

    An episode is a run of failures bounded by a later success. MTTD is
    approximated by time-to-first-detection (the first failing check —
    detection is automatic here, so it equals the poll interval), and
    MTTR by breakage-start → recovery.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            WITH ordered AS (
                SELECT source_id, checked_at,
                       (last_success IS NOT NULL) AS ok,
                       lag(last_success IS NOT NULL)
                         OVER (PARTITION BY source_id ORDER BY checked_at) AS prev_ok
                FROM source_health
                WHERE checked_at > now() - make_interval(days => %s)
            ),
            starts AS (
                SELECT source_id, checked_at AS started_at
                FROM ordered WHERE ok IS FALSE AND coalesce(prev_ok, TRUE) IS TRUE
            ),
            recoveries AS (
                SELECT s.source_id, s.started_at,
                       (SELECT min(o.checked_at) FROM ordered o
                        WHERE o.source_id = s.source_id
                          AND o.ok IS TRUE
                          AND o.checked_at > s.started_at) AS recovered_at
                FROM starts s
            )
            SELECT count(*) AS episodes,
                   count(recovered_at) AS resolved,
                   avg(extract(epoch FROM (recovered_at - started_at)) / 60.0)
                     AS avg_mttr_min,
                   max(extract(epoch FROM (recovered_at - started_at)) / 60.0)
                     AS max_mttr_min
            FROM recoveries
            """,
            (days,),
        )
        row = dict(cur.fetchone())

    return {
        "window_days": days,
        "episodes": row["episodes"],
        "resolved": row["resolved"],
        "open": row["episodes"] - row["resolved"],
        "avg_mttr_min": round(row["avg_mttr_min"], 1) if row["avg_mttr_min"] else None,
        "max_mttr_min": round(row["max_mttr_min"], 1) if row["max_mttr_min"] else None,
        "mttr_target_min": 1440,   # §16: < 24 h
        "mttr_met": (row["avg_mttr_min"] or 0) <= 1440,
    }
