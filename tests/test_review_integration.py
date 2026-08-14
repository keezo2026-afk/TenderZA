"""Review-queue resolution integration tests — real Postgres via FastAPI
TestClient. Skipped without TEST_DATABASE_URL."""

import os
import uuid
from datetime import datetime, timezone

import pytest

psycopg = pytest.importorskip("psycopg")
fastapi_testclient = pytest.importorskip("fastapi.testclient")

from psycopg.types.json import Jsonb  # noqa: E402

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="TEST_DATABASE_URL not set (integration tests need Postgres)"
)


@pytest.fixture()
def client():
    """TestClient with a real pool bound to the test database."""
    os.environ["DATABASE_URL"] = DSN
    from tenderza.api.app import app
    with fastapi_testclient.TestClient(app) as c:
        yield c


@pytest.fixture()
def queued_item():
    """A tender with an unverified closing date + a queued review item."""
    uniq = uuid.uuid4().hex[:8]
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tenders (title, tender_number, status, closing_at,
                                 field_provenance, original_url)
            VALUES (%s, %s, 'UNKNOWN', %s, %s, 'https://src.example/t')
            RETURNING id
            """,
            (
                f"Review Test Tender {uniq}",
                f"RVW {uniq}",
                datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc),
                Jsonb({"closing_at": {"source": "DERIVED",
                                      "source_id": "doc:abc", "confidence": 0.6}}),
            ),
        )
        tender_id = str(cur.fetchone()[0])
        cur.execute(
            "INSERT INTO tender_versions (tender_id, version_no, changes) "
            "VALUES (%s, 1, '{}')",
            (tender_id,),
        )
        cur.execute(
            """
            INSERT INTO review_queue (tender_id, field, value, confidence)
            VALUES (%s, 'closing_at', %s, 0.6)
            RETURNING id
            """,
            (tender_id, Jsonb({"value": "2026-09-30T11:00:00+02:00",
                               "confidence": 0.6,
                               "evidence": "Closing Date: 30 September 2026"})),
        )
        item_id = str(cur.fetchone()[0])
        conn.commit()
    return {"tender_id": tender_id, "item_id": item_id}


def _tender_state(tender_id):
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT closing_at, field_provenance FROM tenders WHERE id = %s",
            (tender_id,),
        )
        closing_at, prov = cur.fetchone()
        cur.execute(
            "SELECT version_no, change_kind FROM tender_versions "
            "WHERE tender_id = %s ORDER BY version_no DESC LIMIT 1",
            (tender_id,),
        )
        latest_version = cur.fetchone()
    return closing_at, prov, latest_version


class TestListAndStats:
    def test_open_items_listed_with_context(self, client, queued_item):
        res = client.get("/review")
        assert res.status_code == 200
        items = res.json()["items"]
        mine = next(i for i in items if i["id"] == queued_item["item_id"])
        assert mine["field"] == "closing_at"
        assert mine["extracted"]["evidence"].startswith("Closing Date")
        assert mine["tender"]["verify_at_source"] == "https://src.example/t"

    def test_stats(self, client, queued_item):
        res = client.get("/review/stats")
        assert res.status_code == 200
        body = res.json()
        assert body["open"] >= 1
        assert "closing_at" in body["open_by_field"]


class TestResolve:
    def test_approve_applies_value_with_human_provenance(self, client, queued_item):
        res = client.post(f"/review/{queued_item['item_id']}/resolve",
                          json={"action": "approve", "reviewer": "keezo"})
        assert res.status_code == 200, res.text

        closing_at, prov, latest = _tender_state(queued_item["tender_id"])
        assert prov["closing_at"]["confidence"] == 1.0
        assert prov["closing_at"]["source_id"] == "human:keezo"
        assert latest[1] == "HUMAN_VERIFIED"          # version row written (§9)
        assert closing_at.isoformat().startswith("2026-09-30")

        # Verified closing date unlocks real status (§10.3): no longer UNKNOWN
        with psycopg.connect(DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT status::text FROM tenders WHERE id = %s",
                        (queued_item["tender_id"],))
            assert cur.fetchone()[0] in ("OPEN", "CLOSING_SOON", "CLOSED")

    def test_correct_overrides_with_reviewer_value(self, client, queued_item):
        res = client.post(
            f"/review/{queued_item['item_id']}/resolve",
            json={"action": "correct",
                  "corrected_value": "2026-10-15T11:00:00+02:00"},
        )
        assert res.status_code == 200, res.text
        closing_at, prov, latest = _tender_state(queued_item["tender_id"])
        assert closing_at == datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)
        assert prov["closing_at"]["confidence"] == 1.0
        assert latest[1] == "HUMAN_VERIFIED"

    def test_reject_keeps_tender_untouched(self, client, queued_item):
        before = _tender_state(queued_item["tender_id"])
        res = client.post(f"/review/{queued_item['item_id']}/resolve",
                          json={"action": "reject"})
        assert res.status_code == 200
        after = _tender_state(queued_item["tender_id"])
        assert before == after                        # nothing changed

    def test_double_resolve_404s(self, client, queued_item):
        first = client.post(f"/review/{queued_item['item_id']}/resolve",
                            json={"action": "reject"})
        assert first.status_code == 200
        second = client.post(f"/review/{queued_item['item_id']}/resolve",
                             json={"action": "approve"})
        assert second.status_code == 404

    def test_correct_without_value_422s(self, client, queued_item):
        res = client.post(f"/review/{queued_item['item_id']}/resolve",
                          json={"action": "correct"})
        assert res.status_code == 422

    def test_resolved_item_leaves_queue(self, client, queued_item):
        client.post(f"/review/{queued_item['item_id']}/resolve",
                    json={"action": "approve"})
        ids = [i["id"] for i in client.get("/review").json()["items"]]
        assert queued_item["item_id"] not in ids
