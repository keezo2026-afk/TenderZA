"""Alert engine integration tests — real Postgres + dev outbox.
Skipped without TEST_DATABASE_URL."""

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.types.json import Jsonb  # noqa: E402

from tenderza.alerts.engine import run_alerts  # noqa: E402

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="TEST_DATABASE_URL not set (integration tests need Postgres)"
)

SAST = timezone(timedelta(hours=2))


@pytest.fixture()
def outbox(tmp_path, monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.setenv("ALERT_OUTBOX", str(tmp_path))
    return tmp_path


@pytest.fixture()
def conn():
    with psycopg.connect(DSN) as c:
        yield c
        c.rollback()


def _mk_tender(conn, *, title, keywords_hit=True, province="KwaZulu-Natal",
               verified=True, status="OPEN", closing_days=10):
    uniq = uuid.uuid4().hex[:8]
    prov = {"closing_at": {"source": "SOURCE" if verified else "INFERRED",
                           "source_id": "test", "confidence": 0.98 if verified else 0.5}}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tenders (title, tender_number, status, province,
                                 closing_at, field_provenance, original_url)
            VALUES (%s, %s, %s::tender_status, %s, %s, %s, 'https://src/t')
            RETURNING id
            """,
            (title if keywords_hit else f"Unrelated thing {uniq}",
             f"AL {uniq}", status, province,
             datetime.now(timezone.utc) + timedelta(days=closing_days),
             Jsonb(prov)),
        )
        return str(cur.fetchone()[0])


def _mk_alert(conn, *, email=None, keywords="cctv", provinces=None,
              closing_within_days=None):
    email = email or f"user-{uuid.uuid4().hex[:8]}@example.co.za"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, name) VALUES (%s, 'Test User') "
            "ON CONFLICT (email) DO UPDATE SET name = users.name RETURNING id",
            (email,),
        )
        user_id = cur.fetchone()[0]
        prefs = {"closing_within_days": closing_within_days} if closing_within_days else {}
        cur.execute(
            """
            INSERT INTO user_alerts (user_id, keywords, provinces, categories,
                                     batch_prefs, channels, active)
            VALUES (%s, %s, %s, '[]', %s, '["email"]', true)
            RETURNING id
            """,
            (user_id, keywords, Jsonb(provinces or []), Jsonb(prefs)),
        )
        return str(cur.fetchone()[0]), email


class TestEngine:
    def test_digest_sent_once_batched(self, conn, outbox):
        _mk_tender(conn, title="CCTV camera installation A")
        _mk_tender(conn, title="CCTV camera installation B")
        alert_id, email = _mk_alert(conn, keywords="cctv camera installation")
        conn.commit()

        summary = run_alerts(conn)
        my_run = next(r for r in summary.runs if r.alert_id == alert_id)
        assert my_run.matched >= 2 and my_run.sent

        # ONE email containing BOTH tenders (batching, §14)
        emls = list(outbox.glob("*.eml"))
        mine = [e for e in emls if email.replace("@", "_at_") in e.name]
        assert len(mine) == 1
        import email.parser
        msg = email.parser.Parser().parsestr(mine[0].read_text())
        body = msg.get_payload(decode=True).decode()
        assert "installation A" in body and "installation B" in body

    def test_never_notified_twice(self, conn, outbox):
        _mk_tender(conn, title="CCTV perimeter upgrade once-only")
        alert_id, email = _mk_alert(conn, keywords="perimeter upgrade once-only")
        conn.commit()

        first = run_alerts(conn)
        second = run_alerts(conn)
        r1 = next(r for r in first.runs if r.alert_id == alert_id)
        r2 = next(r for r in second.runs if r.alert_id == alert_id)
        assert r1.sent and r1.matched >= 1
        assert r2.matched == 0 and not r2.sent      # nothing new -> silence

    def test_deadline_alert_excludes_unverified_dates(self, conn, outbox):
        t_ver = _mk_tender(conn, title="Verified deadline fencing works",
                           verified=True, closing_days=5)
        t_unv = _mk_tender(conn, title="Unverified deadline fencing works",
                           verified=False, closing_days=5)
        alert_id, _ = _mk_alert(conn, keywords="deadline fencing works",
                                closing_within_days=7)
        conn.commit()

        run_alerts(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tender_id::text FROM alert_events WHERE alert_id = %s",
                (alert_id,),
            )
            notified = {r[0] for r in cur.fetchall()}
        assert t_ver in notified
        assert t_unv not in notified                 # §14 hard rule

    def test_closed_tenders_never_alert(self, conn, outbox):
        _mk_tender(conn, title="Closed bridge repair works", status="CLOSED")
        alert_id, _ = _mk_alert(conn, keywords="closed bridge repair works")
        conn.commit()
        summary = run_alerts(conn)
        my_run = next(r for r in summary.runs if r.alert_id == alert_id)
        assert my_run.matched == 0

    def test_province_filter(self, conn, outbox):
        _mk_tender(conn, title="Gauteng-only paving tender",
                   province="Gauteng")
        alert_id, _ = _mk_alert(conn, keywords="paving",
                                provinces=["KwaZulu-Natal"])
        conn.commit()
        summary = run_alerts(conn)
        my_run = next(r for r in summary.runs if r.alert_id == alert_id)
        assert my_run.matched == 0                   # wrong province

    def test_paused_alert_skipped(self, conn, outbox):
        _mk_tender(conn, title="Paused-alert generator maintenance")
        alert_id, _ = _mk_alert(conn, keywords="paused-alert generator")
        with conn.cursor() as cur:
            cur.execute("UPDATE user_alerts SET active = false WHERE id = %s",
                        (alert_id,))
        conn.commit()
        summary = run_alerts(conn)
        assert all(r.alert_id != alert_id for r in summary.runs)


class TestAlertsApi:
    @pytest.fixture()
    def client(self):
        os.environ["DATABASE_URL"] = DSN
        from fastapi.testclient import TestClient

        from tenderza.api.app import app
        with TestClient(app) as c:
            yield c

    def test_create_list_toggle(self, client):
        email = f"api-{uuid.uuid4().hex[:8]}@example.co.za"
        res = client.post("/alerts", json={
            "email": email, "keywords": "solar geysers",
            "provinces": ["KwaZulu-Natal"], "closing_within_days": 30,
        })
        assert res.status_code == 200, res.text
        alert_id = res.json()["alert_id"]

        listed = client.get("/alerts", params={"email": email}).json()
        assert len(listed["alerts"]) == 1
        assert listed["alerts"][0]["closing_within_days"] == 30

        toggled = client.post(f"/alerts/{alert_id}/toggle").json()
        assert toggled["active"] is False

    def test_empty_criteria_rejected(self, client):
        res = client.post("/alerts", json={"email": "x@example.co.za"})
        assert res.status_code == 422

    def test_bad_province_rejected(self, client):
        res = client.post("/alerts", json={
            "email": "x@example.co.za", "keywords": "x",
            "provinces": ["Atlantis"],
        })
        assert res.status_code == 422
