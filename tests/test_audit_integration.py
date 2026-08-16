"""Audit trail tests (Blueprint §17).

The audit log is only worth having if it records the things an incident
responder actually needs: successful logins, *failed* logins with the reason
the user was never told, privilege grants, and data edits. These tests pin
that content down, plus the two properties that make the log trustworthy:
it never breaks the request that it is logging, and reads are admin-only.
"""

import os
import uuid

import pytest

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")

PASSWORD = "audit-test-password-1"


@pytest.fixture()
def conn():
    import psycopg
    with psycopg.connect(DSN) as c:
        yield c


@pytest.fixture()
def client():
    os.environ["DATABASE_URL"] = DSN
    os.environ["TENDERZA_AUTH"] = "on"
    from fastapi.testclient import TestClient

    from tenderza.api.app import app
    with TestClient(app) as c:
        yield c


def _make_user(conn, role="viewer", email=None):
    from tenderza.auth.passwords import hash_password
    address = email or f"audit-{uuid.uuid4().hex[:8]}@tests.example.com"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, name, password_hash, role) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (address, "audit subject", hash_password(PASSWORD), role),
        )
        uid = str(cur.fetchone()[0])
    conn.commit()
    return uid, address


def _entries(conn, **kw):
    from tenderza import audit
    conn.rollback()          # see the writes committed by the API
    return audit.recent(conn, **kw)


class TestWriting:
    def test_record_is_visible_after_commit(self, conn):
        from tenderza import audit
        uid, email = _make_user(conn)
        audit.record(conn, table_name="users", action=audit.LOGIN_OK,
                     record_id=uid, user_id=uid, detail={"role": "viewer"})
        conn.commit()

        entry = _entries(conn, email=email)[0]
        assert entry["action"] == audit.LOGIN_OK
        assert entry["actor"] == email
        assert entry["actor_role"] == "viewer"
        assert entry["detail"]["role"] == "viewer"

    def test_a_broken_write_never_raises(self, conn):
        """Auditing is observability. If it fails, the user's action must
        still succeed -- so record() swallows its own errors."""
        from tenderza import audit

        class Exploding:
            def cursor(self, *a, **k):
                raise RuntimeError("audit table is on fire")

        audit.record(Exploding(), table_name="users", action="whatever")

    def test_synthetic_principal_writes_null_user_id(self, conn):
        """The service token has a UUID-shaped id that matches no users row;
        writing it into the FK column would fail (same rule as reviewed_by)."""
        from tenderza import audit
        from tenderza.auth.deps import SERVICE_PRINCIPAL

        marker = f"synthetic-{uuid.uuid4().hex[:8]}"
        audit.record_for_principal(
            conn, SERVICE_PRINCIPAL, table_name="users",
            action=audit.ROLE_CHANGED, detail={"email": marker},
        )
        conn.commit()

        entry = _entries(conn, email=marker)[0]
        assert entry["actor"] == SERVICE_PRINCIPAL.email
        assert entry["detail"]["email"] == marker

    def test_rolled_back_action_leaves_no_audit_row(self, conn):
        """The audit row rides in the caller's transaction, so an action that
        is rolled back does not leave a phantom record claiming it happened."""
        from tenderza import audit
        marker = f"rollback-{uuid.uuid4().hex[:8]}"
        audit.record(conn, table_name="users", action=audit.ROLE_CHANGED,
                     detail={"email": marker})
        conn.rollback()
        assert _entries(conn, email=marker) == []


class TestLoginTrail:
    def test_successful_login_is_recorded(self, client, conn):
        from tenderza import audit
        uid, email = _make_user(conn, role="analyst")

        assert client.post("/auth/login",
                           json={"email": email, "password": PASSWORD}
                           ).status_code == 200

        entry = _entries(conn, email=email, action=audit.LOGIN_OK)[0]
        assert entry["actor"] == email
        assert entry["detail"]["role"] == "analyst"
        assert entry["record_id"] == uid

    def test_failed_login_records_the_reason_the_caller_never_sees(
            self, client, conn):
        """The API returns a deliberately uniform 401 to avoid an enumeration
        oracle -- but the operator still needs to know which failure it was."""
        from tenderza import audit
        uid, email = _make_user(conn)

        res = client.post("/auth/login",
                          json={"email": email, "password": "wrong-password"})
        assert res.status_code == 401
        assert res.json()["detail"] == "invalid email or password"

        entry = _entries(conn, email=email, action=audit.LOGIN_FAIL)[0]
        assert entry["detail"]["reason"] == "bad_password"

    def test_login_attempt_on_unknown_account_is_recorded(self, client, conn):
        from tenderza import audit
        ghost = f"ghost-{uuid.uuid4().hex[:8]}@tests.example.com"
        client.post("/auth/login", json={"email": ghost, "password": "x" * 12})

        entry = _entries(conn, email=ghost, action=audit.LOGIN_FAIL)[0]
        assert entry["detail"]["reason"] == "no_such_user"
        # No users row exists, so attribution lives in detail.actor only.
        assert entry["actor"] == ghost
        assert entry["detail"]["actor"] == ghost

    def test_disabled_account_is_distinguishable_in_the_log(self, client, conn):
        from tenderza import audit
        uid, email = _make_user(conn)
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET disabled_at = now() WHERE id = %s",
                        (uid,))
        conn.commit()

        assert client.post("/auth/login",
                           json={"email": email, "password": PASSWORD}
                           ).status_code == 401
        entry = _entries(conn, email=email, action=audit.LOGIN_FAIL)[0]
        assert entry["detail"]["reason"] == "disabled"

    def test_password_change_records_the_containment_sweep(self, client, conn):
        from tenderza import audit
        uid, email = _make_user(conn)
        client.post("/auth/login", json={"email": email, "password": PASSWORD})
        res = client.post("/auth/password",
                          json={"current_password": PASSWORD,
                                "new_password": "a-brand-new-password"})
        assert res.status_code == 200, res.text

        entry = _entries(conn, email=email, action=audit.PASSWORD_CHANGED)[0]
        assert entry["detail"]["sessions_revoked"] >= 1

    def test_no_secret_material_is_ever_logged(self, client, conn):
        """A password typed into the wrong box must not be preserved forever
        in the audit table."""
        uid, email = _make_user(conn)
        secret = "hunter2-do-not-log-this"
        client.post("/auth/login", json={"email": email, "password": secret})
        client.post("/auth/login", json={"email": email, "password": PASSWORD})

        blob = str(_entries(conn, email=email))
        assert secret not in blob
        assert PASSWORD not in blob
        assert "scrypt" not in blob          # no password hashes either


class TestReadEndpoint:
    def test_audit_endpoint_is_admin_only(self, client, conn, sign_in):
        assert client.get("/ops/audit").status_code in (401, 403)

        sign_in(client, "analyst")
        assert client.get("/ops/audit").status_code == 403

        client.post("/auth/logout")
        sign_in(client, "admin")
        res = client.get("/ops/audit")
        assert res.status_code == 200
        assert isinstance(res.json()["entries"], list)

    def test_filters_and_limit(self, client, conn, sign_in):
        from tenderza import audit
        uid, email = _make_user(conn)
        client.post("/auth/login", json={"email": email, "password": "nope"})
        sign_in(client, "admin")

        res = client.get("/ops/audit",
                         params={"action": audit.LOGIN_FAIL, "email": email})
        body = res.json()
        assert body["count"] >= 1
        assert {e["action"] for e in body["entries"]} == {audit.LOGIN_FAIL}

        capped = client.get("/ops/audit", params={"limit": 1}).json()
        assert capped["count"] <= 1
        assert client.get("/ops/audit", params={"limit": 9999}).status_code == 422

    def test_newest_first(self, client, conn, sign_in):
        from tenderza import audit
        uid, email = _make_user(conn)
        for reason in ("first", "second"):
            audit.record(conn, table_name="users", action=audit.ROLE_CHANGED,
                         user_id=uid, detail={"note": reason})
            conn.commit()

        entries = _entries(conn, email=email, action=audit.ROLE_CHANGED)
        assert entries[0]["detail"]["note"] == "second"


class TestReviewTrail:
    def test_resolving_a_review_item_is_audited(self, client, conn, sign_in):
        """The edit and the record of who made it must commit together."""
        from tenderza import audit
        from tests.test_review_integration import _seed_queued_item

        sign_in(client, "analyst")
        seeded = _seed_queued_item()
        res = client.post(f"/review/{seeded['item_id']}/resolve",
                          json={"action": "approve"})
        assert res.status_code == 200, res.text

        entry = _entries(conn, action=audit.REVIEW_RESOLVED)[0]
        assert entry["record_id"] == seeded["item_id"]
        assert entry["detail"]["decision"] == "approve"
        assert entry["detail"]["field"] == "closing_at"
        assert entry["actor_role"] == "analyst"
