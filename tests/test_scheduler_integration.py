"""Scheduler integration tests — state machine on real Postgres.

Uses a test-only fake adapter (no network). Skipped without TEST_DATABASE_URL;
CI runs them against the pgvector Postgres service.
"""

import os
from datetime import datetime, timezone

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.types.json import Jsonb  # noqa: E402

from tenderza.adapters.base import (  # noqa: E402
    Adapter,
    RawTenderNotice,
    register_adapter,
)
from tenderza.crawl.scheduler import (  # noqa: E402
    MAX_ATTEMPTS,
    enqueue_due_sources,
    run_pending,
)

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="TEST_DATABASE_URL not set (integration tests need Postgres)"
)


# One-time registration of test adapters (module import runs once per session).
@register_adapter
class _FakeGoodAdapter(Adapter):
    key = "test_good"

    def fetch(self):
        yield RawTenderNotice(
            source_id=self.config.source_id,
            source_url="https://fake.gov.za/t/1",
            title="Fake Municipal Tender: Road Repairs Ward 3",
            tender_number="FAKE 001/2026",
            buyer_name="Fake Municipality",
            closing_at=datetime(2026, 12, 1, 11, 0, tzinfo=timezone.utc),
            raw={"format": "feed"},
        )


@register_adapter
class _FakeBrokenAdapter(Adapter):
    key = "test_broken"

    def fetch(self):
        raise ConnectionError("simulated site down")
        yield  # pragma: no cover


@pytest.fixture()
def conn():
    with psycopg.connect(DSN) as c:
        yield c
        c.rollback()


def _make_source(conn, *, adapter: str, name: str, frequency_min: int = 60) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sources (name, crawl_url, adapter, adapter_config,
                                 status, frequency_min, authority_score)
            VALUES (%s, 'https://fake.gov.za/tenders', %s, %s, 'ACTIVE', %s, 95)
            RETURNING id
            """,
            (name, adapter, Jsonb({}), frequency_min),
        )
        return str(cur.fetchone()[0])


class TestEnqueue:
    def test_never_crawled_source_is_due(self, conn):
        _make_source(conn, adapter="test_good", name="Enq A")
        assert enqueue_due_sources(conn) >= 1

    def test_no_duplicate_live_jobs(self, conn):
        _make_source(conn, adapter="test_good", name="Enq B")
        first = enqueue_due_sources(conn)
        assert first >= 1
        assert enqueue_due_sources(conn) == 0  # live job exists -> skip

    def test_recently_checked_not_due(self, conn):
        sid = _make_source(conn, adapter="test_good", name="Enq C")
        with conn.cursor() as cur:
            cur.execute("UPDATE sources SET last_checked = now() WHERE id = %s", (sid,))
        assert enqueue_due_sources(conn) == 0


class TestRunJob:
    def test_happy_path_done_and_persisted(self, conn):
        sid = _make_source(conn, adapter="test_good", name="Run A")
        enqueue_due_sources(conn)
        outcomes = run_pending(conn)
        assert len(outcomes) == 1
        o = outcomes[0]
        assert o.state == "DONE"
        assert o.notices == 1 and o.created == 1

        with conn.cursor() as cur:
            # tender landed through the same pipeline (dedupe fields set)
            cur.execute(
                "SELECT normalized_tender_number FROM tenders "
                "WHERE tender_number = 'FAKE 001/2026'"
            )
            assert cur.fetchone()[0] == "fake12026"
            # append-only crawl result recorded
            cur.execute(
                "SELECT count(*) FROM crawl_results r JOIN crawl_jobs j "
                "ON j.id = r.job_id WHERE j.source_id = %s", (sid,),
            )
            assert cur.fetchone()[0] == 1
            # source freshness maintained
            cur.execute(
                "SELECT last_success IS NOT NULL, status::text FROM sources "
                "WHERE id = %s", (sid,),
            )
            ok, status = cur.fetchone()
            assert ok and status == "ACTIVE"

    def test_rerun_is_idempotent(self, conn):
        sid = _make_source(conn, adapter="test_good", name="Run B")
        enqueue_due_sources(conn)
        run_pending(conn)
        # force due again
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sources SET last_checked = now() - interval '2 hours' "
                "WHERE id = %s", (sid,),
            )
        enqueue_due_sources(conn)
        outcomes = run_pending(conn)
        assert outcomes[0].state == "DONE"
        assert outcomes[0].created == 0  # same tender -> no duplicate

    def test_failure_backs_off_then_fails_terminally(self, conn):
        sid = _make_source(conn, adapter="test_broken", name="Run C")
        enqueue_due_sources(conn)

        outcomes = run_pending(conn)
        assert outcomes[0].state == "RETRY_BACKOFF"
        assert "simulated site down" in outcomes[0].error

        with conn.cursor() as cur:
            cur.execute("SELECT status::text FROM sources WHERE id = %s", (sid,))
            assert cur.fetchone()[0] == "DEGRADED"

        # Exhaust the remaining attempts (mature the backoff each time).
        for attempt in range(2, MAX_ATTEMPTS + 1):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE crawl_jobs SET next_retry_at = now() "
                    "WHERE source_id = %s AND state = 'RETRY_BACKOFF'", (sid,),
                )
            outcomes = run_pending(conn)
            expected = "FAILED" if attempt == MAX_ATTEMPTS else "RETRY_BACKOFF"
            assert outcomes[0].state == expected, f"attempt {attempt}"

        with conn.cursor() as cur:
            cur.execute("SELECT status::text FROM sources WHERE id = %s", (sid,))
            assert cur.fetchone()[0] == "FAILED"
            cur.execute(
                "SELECT count(*) FROM source_health WHERE source_id = %s", (sid,),
            )
            assert cur.fetchone()[0] == MAX_ATTEMPTS  # every run logged
