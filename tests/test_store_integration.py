"""TenderStore integration tests — require a real Postgres with db/schema.sql.

Skipped when TEST_DATABASE_URL is unset. CI provides a pgvector Postgres
service and runs these (see ci/github-ci.yml). Locally:

    docker compose -f infra/docker-compose.yml up -d
    psql postgresql://tenderza:tenderza@localhost:5432/tenderza -f db/schema.sql
    TEST_DATABASE_URL=postgresql://tenderza:tenderza@localhost:5432/tenderza pytest
"""

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from tenderza.adapters.base import RawTenderNotice  # noqa: E402
from tenderza.adapters.ocds_api import release_to_notice  # noqa: E402
from tenderza.persistence import TenderStore  # noqa: E402
from tenderza.pipeline import normalize_notice  # noqa: E402
from tenderza.pipeline.normalizer import SAST  # noqa: E402

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="TEST_DATABASE_URL not set (integration tests need Postgres)"
)

FIXTURES = Path(__file__).parent / "fixtures" / "adapters"


@pytest.fixture()
def conn():
    with psycopg.connect(DSN) as c:
        yield c
        c.rollback()  # every test runs in a rolled-back transaction


@pytest.fixture()
def store(conn):
    return TenderStore(conn)


def _notice(**kw) -> RawTenderNotice:
    defaults = dict(
        source_id="etenders-ocds",
        source_url="https://a.gov.za/t/1",
        title="Supply and Installation of CCTV System",
        tender_number="SCM 045/2026",
        buyer_name="eThekwini Metropolitan Municipality",
        closing_at=datetime(2026, 9, 15, 11, 0, tzinfo=SAST),
    )
    defaults.update(kw)
    return RawTenderNotice(**defaults)


class TestUpsert:
    def test_insert_creates_version_1(self, store):
        t = normalize_notice(_notice(), authority_score=100)
        result = store.upsert_tender(t)
        assert result.created
        assert result.version_no == 1
        assert result.change_kind == "CREATED"

    def test_reingest_is_noop(self, store):
        t = normalize_notice(_notice(), authority_score=100)
        first = store.upsert_tender(t)
        again = store.upsert_tender(normalize_notice(_notice(), authority_score=100))
        assert again.tender_id == first.tender_id
        assert not again.created
        assert not again.changed

    def test_closing_extension_creates_version_with_kind(self, store):
        store.upsert_tender(normalize_notice(_notice(), authority_score=100))
        extended = normalize_notice(
            _notice(closing_at=datetime(2026, 10, 1, 11, 0, tzinfo=SAST)),
            authority_score=100,
        )
        result = store.upsert_tender(extended)
        assert result.changed
        assert result.version_no == 2
        assert result.change_kind == "EXTENDED"

    def test_cross_source_duplicate_merges_attribution(self, store, conn):
        store.upsert_tender(normalize_notice(_notice(), authority_score=100))
        dup = normalize_notice(
            _notice(
                source_id="aggregator-x",
                source_url="https://aggregator.example/t/9",
                tender_number="SCM45/2026",       # different formatting
                buyer_name="City of eThekwini",   # different alias
            ),
            authority_score=70,
        )
        # NOTE: alias must resolve to the same buyer key for the fingerprint
        # to collide — this asserts the §7 chain end-to-end.
        result = store.upsert_tender(dup)
        assert not result.created
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_urls FROM tenders WHERE id = %s", (result.tender_id,)
            )
            urls = cur.fetchone()[0]
        assert "https://aggregator.example/t/9" in urls
        assert "https://a.gov.za/t/1" in urls


class TestOcdsArchive:
    def test_archive_verbatim_and_idempotent(self, store, conn):
        release = json.loads(
            (FIXTURES / "ocds_api" / "real_release_2026-08-14.json").read_text()
        )["release"]
        assert store.archive_ocds_release(release) is True
        assert store.archive_ocds_release(release) is False  # conflict-ignored
        with conn.cursor() as cur:
            cur.execute(
                "SELECT raw FROM ocds_records WHERE ocid = %s", (release["ocid"],)
            )
            stored = cur.fetchone()[0]
        assert stored == release  # verbatim (§10.2.6)


class TestEntityResolution:
    def test_buyer_created_once_across_aliases(self, store, conn):
        store.upsert_tender(normalize_notice(
            _notice(title="Tender A", tender_number="A1/2026")))
        store.upsert_tender(normalize_notice(
            _notice(title="Tender B", tender_number="B2/2026",
                    buyer_name="City of eThekwini")))
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM organisations WHERE name ILIKE '%ethekwini%'"
            )
            assert cur.fetchone()[0] == 1  # one org, two aliases


class TestReviewQueue:
    def test_low_confidence_lands_in_review_queue(self, store, conn):
        t = normalize_notice(_notice(), structured=False)  # low confidence
        assert t.review_items
        result = store.upsert_tender(t)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT field FROM review_queue "
                "WHERE tender_id = %s AND resolved_at IS NULL",
                (result.tender_id,),
            )
            fields = {row[0] for row in cur.fetchall()}
        assert "closing_at" in fields


class TestEndToEnd:
    def test_real_release_persists(self, store):
        release = json.loads(
            (FIXTURES / "ocds_api" / "real_release_2026-08-14.json").read_text()
        )["release"]
        store.archive_ocds_release(release)
        notice = release_to_notice(
            release, "etenders-ocds", "https://ocds-api.etenders.gov.za/api/OCDSReleases"
        )
        result = store.upsert_tender(normalize_notice(notice, authority_score=100))
        assert result.created
        counts = store.counts()
        assert counts["tenders"] >= 1
        assert counts["ocds_records"] >= 1
