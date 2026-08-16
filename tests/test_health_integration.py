"""Source-health queries on real Postgres (Blueprint §15, §16).

Verifies that the health SQL reconstructs breakage episodes correctly from
the append-only source_health log — the part that can't be unit-tested,
because the episode/window logic lives in SQL.

Skipped without TEST_DATABASE_URL; CI runs it against pgvector Postgres.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402

from tenderza.health.queries import (  # noqa: E402
    adapter_health,
    crawl_stats,
    fetch_source_rows,
    freshness_by_tier,
    mttd_mttr_report,
    recent_failures,
    source_verdicts,
)

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="TEST_DATABASE_URL not set (integration tests need Postgres)"
)

NOW = datetime.now(timezone.utc)


@pytest.fixture()
def conn():
    with psycopg.connect(DSN) as c:
        yield c
        c.rollback()


def _source(conn, name, *, tier=2, status="ACTIVE", adapter="test_good",
            frequency_min=60, last_success=None):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sources (name, crawl_url, adapter, adapter_config, status,
                                 triage_tier, frequency_min, authority_score,
                                 last_success, last_checked)
            VALUES (%s, 'https://health.test/tenders', %s, %s, %s::source_status,
                    %s, %s, 90, %s, now())
            RETURNING id
            """,
            (name, adapter, Jsonb({}), status, tier, frequency_min, last_success),
        )
        return str(cur.fetchone()[0])


def _health(conn, source_id, *, ok: bool, at: datetime, error: str | None = None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO source_health (source_id, checked_at, last_success, last_error) "
            "VALUES (%s, %s, %s, %s)",
            (source_id, at, at if ok else None, None if ok else (error or "boom")),
        )


def _verdict_for(conn, source_id):
    return next(v for v in source_verdicts(conn, now=NOW)
                if v.source_id == source_id)


class TestEpisodeReconstruction:
    def test_failures_after_last_success_form_one_open_episode(self, conn):
        sid = _source(conn, "Episode A", tier=1, frequency_min=30,
                      last_success=NOW - timedelta(hours=8))
        _health(conn, sid, ok=True, at=NOW - timedelta(hours=8))
        for h in (6, 5, 4):
            _health(conn, sid, ok=False, at=NOW - timedelta(hours=h),
                    error="HTTP 503 from origin")

        row = next(r for r in fetch_source_rows(conn) if str(r["id"]) == sid)
        assert row["consecutive_failures"] == 3
        # Episode starts at the FIRST failure after the last success.
        assert abs((row["breakage_started_at"]
                    - (NOW - timedelta(hours=6))).total_seconds()) < 5
        assert "503" in row["last_error"]

    def test_success_closes_the_episode(self, conn):
        sid = _source(conn, "Episode B", last_success=NOW - timedelta(minutes=10))
        _health(conn, sid, ok=False, at=NOW - timedelta(hours=3))
        _health(conn, sid, ok=False, at=NOW - timedelta(hours=2))
        _health(conn, sid, ok=True, at=NOW - timedelta(minutes=10))

        row = next(r for r in fetch_source_rows(conn) if str(r["id"]) == sid)
        assert row["consecutive_failures"] == 0
        assert row["breakage_started_at"] is None
        assert _verdict_for(conn, sid).state == "OK"

    def test_open_episode_past_mttd_target_pages(self, conn):
        sid = _source(conn, "Episode C", tier=1, frequency_min=30, status="DEGRADED",
                      last_success=NOW - timedelta(hours=9))
        _health(conn, sid, ok=True, at=NOW - timedelta(hours=9))
        for h in (7, 6, 5):
            _health(conn, sid, ok=False, at=NOW - timedelta(hours=h), error="timeout")

        v = _verdict_for(conn, sid)
        assert v.state == "DEGRADED"
        assert v.page is True                      # 7h open > 4h MTTD target (§16)
        assert v.breakage_minutes > 240


class TestVerdictsOverRegistry:
    def test_publish_nothing_never_pages_even_when_never_crawled(self, conn):
        sid = _source(conn, "Dead Muni", tier=4, status="PUBLISH_NOTHING",
                      last_success=None)
        v = _verdict_for(conn, sid)
        assert v.state == "PUBLISH_NOTHING"
        assert v.page is False

    def test_stale_tier1_detected_from_registry_timestamps(self, conn):
        sid = _source(conn, "Stale T1", tier=1, frequency_min=30,
                      last_success=NOW - timedelta(hours=5))
        _health(conn, sid, ok=True, at=NOW - timedelta(hours=5))
        v = _verdict_for(conn, sid)
        assert v.state == "STALE"
        assert v.sla_breached is True
        assert v.page is True

    def test_province_is_joined_for_the_dashboard(self, conn):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO organisations (name, type, province) "
                "VALUES ('Health Test Muni', 'LOCAL', 'KwaZulu-Natal') RETURNING id"
            )
            org = cur.fetchone()[0]
        sid = _source(conn, "Joined Source")
        with conn.cursor() as cur:
            cur.execute("UPDATE sources SET org_id = %s WHERE id = %s", (org, sid))
        row = next(r for r in fetch_source_rows(conn) if str(r["id"]) == sid)
        assert row["province"] == "KwaZulu-Natal"


class TestOpsAggregates:
    def test_crawl_stats_counts_jobs_and_yield(self, conn):
        sid = _source(conn, "Stats Source")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO crawl_jobs (source_id, state, run_time) "
                "VALUES (%s, 'DONE', now()) RETURNING id", (sid,),
            )
            job = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO crawl_results (job_id, raw, extracted_fields) "
                "VALUES (%s, %s, %s)",
                (job, Jsonb({}), Jsonb({"created": 4, "updated": 2})),
            )
        stats = crawl_stats(conn, hours=24)
        assert stats["jobs_total"] >= 1
        assert stats["tenders_created"] >= 4
        assert stats["tenders_updated"] >= 2
        assert 0 <= stats["job_success_pct"] <= 100

    def test_adapter_health_flags_silent_success(self, conn):
        """Runs that succeed but yield nothing = the adapter-rot signature (§19)."""
        sid = _source(conn, "Silent Source", adapter="test_silent")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO crawl_jobs (source_id, state, run_time) "
                "VALUES (%s, 'DONE', now()) RETURNING id", (sid,),
            )
            job = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO crawl_results (job_id, raw, extracted_fields) "
                "VALUES (%s, %s, %s)",
                (job, Jsonb({}), Jsonb({"created": 0, "updated": 0})),
            )
        entry = next(a for a in adapter_health(conn, days=7)
                     if a["adapter"] == "test_silent")
        assert entry["ok"] == 1
        assert entry["created"] == 0
        assert entry["silent"] is True
        assert entry["success_pct"] == 100.0

    def test_freshness_by_tier_reports_sla(self, conn):
        _source(conn, "Fresh T1", tier=1, last_success=NOW - timedelta(minutes=5))
        tiers = {t["triage_tier"]: t for t in freshness_by_tier(conn)}
        assert tiers[1]["sla_minutes"] == 30
        assert tiers[1]["sources"] >= 1

    def test_freshness_excludes_publish_nothing(self, conn):
        _source(conn, "Excluded Muni", tier=3, status="PUBLISH_NOTHING",
                last_success=None)
        rows = freshness_by_tier(conn)
        t3 = next((t for t in rows if t["triage_tier"] == 3), None)
        # Either tier 3 has no rows at all, or our PUBLISH_NOTHING source
        # is not among the counted ones.
        if t3 is not None:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM sources WHERE triage_tier = 3 "
                    "AND status NOT IN ('PUBLISH_NOTHING','INACTIVE')"
                )
                assert t3["sources"] == cur.fetchone()[0]

    def test_recent_failures_lists_newest_first(self, conn):
        sid = _source(conn, "Failing Source")
        _health(conn, sid, ok=False, at=NOW - timedelta(hours=2), error="older error")
        _health(conn, sid, ok=False, at=NOW - timedelta(minutes=5), error="newest error")
        rows = [r for r in recent_failures(conn, limit=50)
                if str(r["source_id"]) == sid]
        assert rows[0]["last_error"] == "newest error"

    def test_mttr_report_measures_closed_episodes(self, conn):
        sid = _source(conn, "Recovered Source", last_success=NOW - timedelta(minutes=1))
        _health(conn, sid, ok=True, at=NOW - timedelta(hours=10))
        _health(conn, sid, ok=False, at=NOW - timedelta(hours=8))
        _health(conn, sid, ok=False, at=NOW - timedelta(hours=7))
        _health(conn, sid, ok=True, at=NOW - timedelta(hours=6))

        rep = mttd_mttr_report(conn, days=30)
        assert rep["episodes"] >= 1
        assert rep["resolved"] >= 1
        assert rep["avg_mttr_min"] is not None
        assert rep["mttr_target_min"] == 1440


class TestEpisodeStamping:
    """The scheduler must stamp mttd_started_at so MTTD/MTTR is a subtraction,
    not an archaeology exercise over the whole health log (§16)."""

    def test_touch_source_opens_carries_and_closes_the_episode(self, conn):
        from tenderza.crawl.scheduler import _touch_source

        sid = _source(conn, "Stamped Source", tier=1)

        _touch_source(conn, sid, ok=False, error="first failure")
        _touch_source(conn, sid, ok=False, error="still down")
        rows = _health_rows(conn, sid)
        assert len(rows) == 2
        assert rows[0]["mttd_started_at"] is not None
        # Second failure carries the SAME episode start forward.
        assert rows[1]["mttd_started_at"] == rows[0]["mttd_started_at"]

        _touch_source(conn, sid, ok=True, note="recovered")
        rows = _health_rows(conn, sid)
        assert rows[-1]["mttd_started_at"] is None      # episode closed
        assert rows[-1]["last_success"] is not None

        # A later failure opens a NEW episode, not the old one.
        _touch_source(conn, sid, ok=False, error="broke again")
        rows = _health_rows(conn, sid)
        assert rows[-1]["mttd_started_at"] is not None
        assert rows[-1]["mttd_started_at"] > rows[0]["mttd_started_at"]

    def test_verdict_uses_the_stamped_episode_start(self, conn):
        sid = _source(conn, "Stamped Verdict", tier=1, status="DEGRADED",
                      last_success=NOW - timedelta(hours=9))
        _health(conn, sid, ok=True, at=NOW - timedelta(hours=9))
        # Backdate a stamped episode start well beyond the T1 MTTD target.
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO source_health (source_id, checked_at, last_error, "
                "mttd_started_at) VALUES (%s, %s, 'down', %s)",
                (sid, NOW - timedelta(hours=1), NOW - timedelta(hours=7)),
            )
        v = _verdict_for(conn, sid)
        assert v.breakage_minutes > 240        # uses the stamp, not checked_at
        assert v.page is True


def _health_rows(conn, source_id):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT checked_at, last_success, last_error, mttd_started_at "
            "FROM source_health WHERE source_id = %s ORDER BY checked_at, id",
            (source_id,),
        )
        return [dict(r) for r in cur.fetchall()]
