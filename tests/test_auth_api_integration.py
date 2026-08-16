"""Authentication and authorisation over real HTTP (§17).

These are the tests that actually prove the deployment gap is closed: every
admin surface is exercised anonymously, as the wrong role, and as the right
role. Skipped without TEST_DATABASE_URL.
"""

import os
import uuid
from datetime import datetime, timezone

import pytest

psycopg = pytest.importorskip("psycopg")
fastapi_testclient = pytest.importorskip("fastapi.testclient")

from psycopg.types.json import Jsonb  # noqa: E402

from tenderza.auth.passwords import hash_password  # noqa: E402
from tenderza.auth.sessions import SESSION_COOKIE  # noqa: E402

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="TEST_DATABASE_URL not set (integration tests need Postgres)"
)

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture(autouse=True)
def _enforce_auth(monkeypatch):
    """Every test in this module runs with enforcement ON unless it says
    otherwise — the dev bypass must never leak into a security test."""
    monkeypatch.setenv("TENDERZA_AUTH", "on")
    monkeypatch.delenv("TENDERZA_ADMIN_TOKEN", raising=False)


@pytest.fixture()
def client():
    os.environ["DATABASE_URL"] = DSN
    from tenderza.api.app import app
    with fastapi_testclient.TestClient(app) as c:
        yield c


def _make_user(email: str, role: str, *, password: str | None = PASSWORD,
               disabled: bool = False) -> str:
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, name, password_hash, role, disabled_at) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (email, email.split("@")[0], hash_password(password) if password else None,
             role, datetime.now(timezone.utc) if disabled else None),
        )
        uid = str(cur.fetchone()[0])
        conn.commit()
    return uid


@pytest.fixture()
def users():
    """One user per role, with unique addresses so runs do not collide."""
    tag = uuid.uuid4().hex[:8]
    made = {
        role: (f"{role}-{tag}@example.com", _make_user(f"{role}-{tag}@example.com", role))
        for role in ("viewer", "analyst", "admin")
    }
    yield {role: email for role, (email, _uid) in made.items()}
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        for _role, (_email, uid) in made.items():
            cur.execute("DELETE FROM users WHERE id = %s", (uid,))
        conn.commit()


def _login(client, email: str, password: str = PASSWORD):
    return client.post("/auth/login", json={"email": email, "password": password})


def _as(client, users, role: str):
    """Log the client in as `role`; the cookie sticks to the TestClient."""
    resp = _login(client, users[role])
    assert resp.status_code == 200, resp.text
    return resp


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------
class TestLogin:
    def test_valid_credentials_set_a_session_cookie(self, client, users):
        resp = _login(client, users["analyst"])
        assert resp.status_code == 200
        assert resp.json()["user"]["role"] == "analyst"
        assert SESSION_COOKIE in resp.cookies

    def test_cookie_is_httponly_and_samesite_lax(self, client, users):
        """HttpOnly stops XSS reading the token; Lax blunts CSRF."""
        resp = _login(client, users["viewer"])
        raw = resp.headers["set-cookie"].lower()
        assert "httponly" in raw
        assert "samesite=lax" in raw

    def test_wrong_password_is_401(self, client, users):
        assert _login(client, users["admin"], "wrong-password").status_code == 401

    def test_unknown_account_is_indistinguishable_from_wrong_password(
            self, client, users):
        """Same status and same body — no account-enumeration oracle."""
        missing = client.post("/auth/login",
                              json={"email": "nobody@example.com",
                                    "password": PASSWORD})
        wrong = _login(client, users["admin"], "wrong-password")
        assert missing.status_code == wrong.status_code == 401
        assert missing.json() == wrong.json()

    def test_disabled_user_cannot_log_in(self, client):
        email = f"disabled-{uuid.uuid4().hex[:8]}@example.com"
        _make_user(email, "admin", disabled=True)
        assert _login(client, email).status_code == 401

    def test_alert_only_contact_cannot_log_in(self, client):
        """password_hash IS NULL means 'we mail this person', not 'account'."""
        email = f"contact-{uuid.uuid4().hex[:8]}@example.com"
        _make_user(email, "viewer", password=None)
        assert _login(client, email, "anything-at-all").status_code == 401

    def test_email_case_is_ignored(self, client, users):
        assert _login(client, users["viewer"].upper()).status_code == 200

    def test_last_login_is_recorded(self, client, users):
        _login(client, users["admin"])
        with psycopg.connect(DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT last_login_at FROM users WHERE lower(email) = %s",
                        (users["admin"],))
            assert cur.fetchone()[0] is not None


# --------------------------------------------------------------------------
# Identity and logout
# --------------------------------------------------------------------------
class TestMeAndLogout:
    def test_anonymous_me_is_200_and_honest(self, client):
        body = client.get("/auth/me").json()
        assert body == {"authenticated": False, "user": None, "auth_required": True}

    def test_me_reports_the_logged_in_user(self, client, users):
        _as(client, users, "analyst")
        body = client.get("/auth/me").json()
        assert body["authenticated"] is True
        assert body["user"]["role"] == "analyst"

    def test_logout_invalidates_the_session(self, client, users):
        _as(client, users, "admin")
        assert client.get("/ops/overview").status_code == 200
        assert client.post("/auth/logout").json()["session_revoked"] is True
        assert client.get("/auth/me").json()["authenticated"] is False
        assert client.get("/ops/overview").status_code == 401

    def test_logout_without_a_session_is_not_an_error(self, client):
        assert client.post("/auth/logout").status_code == 200

    def test_forged_cookie_is_rejected(self, client):
        client.cookies.set(SESSION_COOKIE, "not-a-real-token")
        assert client.get("/auth/me").json()["authenticated"] is False
        assert client.get("/ops/overview").status_code == 401


# --------------------------------------------------------------------------
# The actual gap: admin surfaces
# --------------------------------------------------------------------------
ADMIN_GETS = ["/ops/overview", "/ops/sources", "/ops/alarms", "/ops/crawl",
              "/ops/freshness", "/ops/failures", "/ops/adapters"]
ANALYST_GETS = ["/review", "/review/stats"]


class TestAnonymousIsLockedOut:
    @pytest.mark.parametrize("path", ADMIN_GETS + ANALYST_GETS)
    def test_admin_surfaces_reject_anonymous(self, client, path):
        resp = client.get(path)
        assert resp.status_code == 401, f"{path} leaked to anonymous callers"
        assert resp.headers.get("www-authenticate") == "Bearer"

    def test_alert_run_rejects_anonymous(self, client):
        assert client.post("/alerts/run").status_code == 401

    def test_public_endpoints_stay_public(self, client):
        """Tender data is public procurement information — never gated."""
        for path in ("/health", "/tenders", "/stats"):
            assert client.get(path).status_code == 200


class TestRoleSeparation:
    @pytest.mark.parametrize("path", ADMIN_GETS)
    def test_viewer_cannot_reach_ops(self, client, users, path):
        _as(client, users, "viewer")
        assert client.get(path).status_code == 403

    @pytest.mark.parametrize("path", ADMIN_GETS)
    def test_analyst_cannot_reach_ops(self, client, users, path):
        """Ops exposes crawler internals and source health — admin only."""
        _as(client, users, "analyst")
        assert client.get(path).status_code == 403

    @pytest.mark.parametrize("path", ADMIN_GETS)
    def test_admin_can_reach_ops(self, client, users, path):
        _as(client, users, "admin")
        assert client.get(path).status_code == 200

    @pytest.mark.parametrize("path", ANALYST_GETS)
    def test_viewer_cannot_reach_review(self, client, users, path):
        _as(client, users, "viewer")
        assert client.get(path).status_code == 403

    @pytest.mark.parametrize("path", ANALYST_GETS)
    def test_analyst_can_reach_review(self, client, users, path):
        _as(client, users, "analyst")
        assert client.get(path).status_code == 200

    @pytest.mark.parametrize("path", ANALYST_GETS)
    def test_admin_inherits_analyst_access(self, client, users, path):
        _as(client, users, "admin")
        assert client.get(path).status_code == 200

    def test_alert_run_is_admin_only(self, client, users):
        _as(client, users, "analyst")
        assert client.post("/alerts/run").status_code == 403
        _as(client, users, "admin")
        assert client.post("/alerts/run").status_code == 200


class TestServiceToken:
    def test_bootstrap_token_grants_admin(self, client, monkeypatch):
        """Lets a fresh deployment reach /ops before any user exists."""
        monkeypatch.setenv("TENDERZA_ADMIN_TOKEN", "a-long-bootstrap-secret")
        resp = client.get("/ops/overview",
                          headers={"Authorization": "Bearer a-long-bootstrap-secret"})
        assert resp.status_code == 200

    def test_wrong_token_is_still_locked_out(self, client, monkeypatch):
        monkeypatch.setenv("TENDERZA_ADMIN_TOKEN", "a-long-bootstrap-secret")
        resp = client.get("/ops/overview",
                          headers={"Authorization": "Bearer wrong-secret-value"})
        assert resp.status_code == 401

    def test_token_ignored_when_unconfigured(self, client):
        resp = client.get("/ops/overview",
                          headers={"Authorization": "Bearer anything"})
        assert resp.status_code == 401


# --------------------------------------------------------------------------
# Alerts: ownership, not just authentication
# --------------------------------------------------------------------------
@pytest.fixture()
def alert_of():
    """Create an alert owned by a given email; returns (alert_id, owner)."""
    created = []

    def _make(email: str):
        with psycopg.connect(DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (email) VALUES (%s) "
                "ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email "
                "RETURNING id",
                (email,),
            )
            uid = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO user_alerts (user_id, keywords, provinces, "
                "categories, batch_prefs, channels, active) "
                "VALUES (%s, 'road', %s, %s, %s, '[\"email\"]', true) RETURNING id",
                (uid, Jsonb([]), Jsonb([]), Jsonb({})),
            )
            aid = str(cur.fetchone()[0])
            conn.commit()
        created.append(aid)
        return aid

    yield _make
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        for aid in created:
            cur.execute("DELETE FROM user_alerts WHERE id = %s", (aid,))
        conn.commit()


class TestAlertOwnership:
    def test_signup_stays_open(self, client):
        """Alert creation is the signup funnel — requiring a login first
        would be a chicken-and-egg problem for new users."""
        resp = client.post("/alerts", json={
            "email": f"newsignup-{uuid.uuid4().hex[:8]}@example.com",
            "name": "Road works",
            "keywords": "road resurfacing",
        })
        assert resp.status_code in (200, 201)

    def test_listing_requires_a_session(self, client, users):
        assert client.get("/alerts", params={"email": users["viewer"]}).status_code == 401

    def test_cannot_list_someone_elses_alerts(self, client, users):
        """The IDOR this block exists to close: ?email= used to be enough."""
        _as(client, users, "viewer")
        resp = client.get("/alerts", params={"email": users["analyst"]})
        assert resp.status_code == 403

    def test_can_list_own_alerts(self, client, users):
        alert_owner = users["viewer"]
        _as(client, users, "viewer")
        resp = client.get("/alerts", params={"email": alert_owner})
        assert resp.status_code == 200

    def test_admin_may_list_anyones_alerts(self, client, users):
        _as(client, users, "admin")
        assert client.get("/alerts",
                          params={"email": users["viewer"]}).status_code == 200

    def test_cannot_toggle_someone_elses_alert(self, client, users, alert_of):
        aid = alert_of(users["analyst"])
        _as(client, users, "viewer")
        assert client.post(f"/alerts/{aid}/toggle",
                           json={"active": False}).status_code == 403

    def test_toggling_anonymously_is_refused(self, client, users, alert_of):
        aid = alert_of(users["viewer"])
        assert client.post(f"/alerts/{aid}/toggle",
                           json={"active": False}).status_code == 401

    def test_owner_may_toggle_their_own_alert(self, client, users, alert_of):
        aid = alert_of(users["viewer"])
        _as(client, users, "viewer")
        assert client.post(f"/alerts/{aid}/toggle",
                           json={"active": False}).status_code == 200


# --------------------------------------------------------------------------
# Review: the reviewer is the session, not a form field
# --------------------------------------------------------------------------
@pytest.fixture()
def queued_item():
    uniq = uuid.uuid4().hex[:8]
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tenders (title, tender_number, status, closing_at,
                                 field_provenance, original_url)
            VALUES (%s, %s, 'UNKNOWN', %s, %s, 'https://src.example/t')
            RETURNING id
            """,
            (f"Auth Review Tender {uniq}", f"AUTH {uniq}",
             datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc),
             Jsonb({"closing_at": {"source": "DERIVED", "confidence": 0.6}})),
        )
        tender_id = str(cur.fetchone()[0])
        cur.execute("INSERT INTO tender_versions (tender_id, version_no, changes) "
                    "VALUES (%s, 1, '{}')", (tender_id,))
        cur.execute(
            "INSERT INTO review_queue (tender_id, field, value, confidence) "
            "VALUES (%s, 'closing_at', %s, 0.6) RETURNING id",
            (tender_id, Jsonb({"raw": "2026-09-30T11:00:00+02:00"})),
        )
        item_id = str(cur.fetchone()[0])
        conn.commit()
    yield item_id
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM review_queue WHERE id = %s", (item_id,))
        cur.execute("DELETE FROM tenders WHERE id = %s", (tender_id,))
        conn.commit()


class TestReviewAttribution:
    def test_anonymous_cannot_resolve(self, client, queued_item):
        resp = client.post(f"/review/{queued_item}/resolve",
                           json={"action": "reject"})
        assert resp.status_code == 401

    def test_viewer_cannot_resolve(self, client, users, queued_item):
        _as(client, users, "viewer")
        resp = client.post(f"/review/{queued_item}/resolve",
                           json={"action": "reject"})
        assert resp.status_code == 403

    def test_reviewer_comes_from_the_session_not_the_request(
            self, client, users, queued_item):
        """The old API trusted a free-text `reviewer` field — anyone could
        sign off as anyone. Identity is now taken from the session, and a
        spoofed field in the body is ignored outright."""
        _as(client, users, "analyst")
        resp = client.post(
            f"/review/{queued_item}/resolve",
            json={"action": "reject", "reviewer": "ceo@treasury.gov.za"},
        )
        assert resp.status_code == 200
        with psycopg.connect(DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT resolution FROM review_queue WHERE id = %s",
                        (queued_item,))
            resolution = cur.fetchone()[0]
        assert resolution["reviewer"] == users["analyst"]
        assert "treasury.gov.za" not in str(resolution)

    def test_resolution_records_the_reviewer_foreign_key(
            self, client, users, queued_item):
        """`reviewed_by` was declared but never written — an audit trail that
        pointed at nobody. It now holds the real user id."""
        _as(client, users, "analyst")
        assert client.post(f"/review/{queued_item}/resolve",
                           json={"action": "reject"}).status_code == 200
        with psycopg.connect(DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT u.email FROM review_queue q JOIN users u "
                "ON u.id = q.reviewed_by WHERE q.id = %s", (queued_item,))
            row = cur.fetchone()
        assert row is not None and row[0] == users["analyst"]
