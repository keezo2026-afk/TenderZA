"""Session lifecycle against real Postgres (§17).

Skipped without TEST_DATABASE_URL, like the other integration modules.
"""

import os
from datetime import datetime, timedelta, timezone

import pytest

psycopg = pytest.importorskip("psycopg")

from tenderza.auth.passwords import hash_password  # noqa: E402
from tenderza.auth.sessions import (  # noqa: E402
    create_session,
    hash_token,
    new_token,
    purge_expired,
    resolve_session,
    revoke_all_for_user,
    revoke_session,
)

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="TEST_DATABASE_URL not set (integration tests need Postgres)"
)

NOW = datetime(2026, 8, 16, 9, 0, tzinfo=timezone.utc)


@pytest.fixture()
def conn():
    with psycopg.connect(DSN) as c:
        yield c
        c.rollback()


def _make_user(conn, *, email: str, role: str = "viewer",
               password: str | None = "a-long-enough-password") -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, name, password_hash, role) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (email, email.split("@")[0], hash_password(password) if password else None,
             role),
        )
        return str(cur.fetchone()[0])


class TestCreateAndResolve:
    def test_roundtrip(self, conn):
        uid = _make_user(conn, email="s1@example.com", role="analyst")
        token, expires = create_session(conn, uid, now=NOW)
        principal = resolve_session(conn, token, now=NOW)
        assert principal is not None
        assert principal.user_id == uid
        assert principal.email == "s1@example.com"
        assert principal.role == "analyst"
        assert expires > NOW

    def test_plaintext_token_is_never_stored(self, conn):
        """A database leak must not hand the attacker live sessions."""
        uid = _make_user(conn, email="s2@example.com")
        token, _ = create_session(conn, uid, now=NOW)
        with conn.cursor() as cur:
            cur.execute("SELECT token_hash FROM user_sessions WHERE user_id = %s",
                        (uid,))
            stored = bytes(cur.fetchone()[0])
        assert stored == hash_token(token)
        assert token.encode() not in stored

    def test_tokens_are_unique(self, conn):
        uid = _make_user(conn, email="s3@example.com")
        tokens = {create_session(conn, uid, now=NOW)[0] for _ in range(10)}
        assert len(tokens) == 10

    def test_unknown_token_resolves_to_nobody(self, conn):
        assert resolve_session(conn, new_token(), now=NOW) is None

    def test_empty_and_none_token(self, conn):
        assert resolve_session(conn, None, now=NOW) is None
        assert resolve_session(conn, "", now=NOW) is None

    def test_resolve_touches_last_seen(self, conn):
        uid = _make_user(conn, email="s4@example.com")
        token, _ = create_session(conn, uid, now=NOW)
        later = NOW + timedelta(hours=1)
        resolve_session(conn, token, now=later)
        with conn.cursor() as cur:
            cur.execute("SELECT last_seen_at FROM user_sessions WHERE user_id = %s",
                        (uid,))
            assert cur.fetchone()[0] == later

    def test_user_agent_and_ip_recorded(self, conn):
        uid = _make_user(conn, email="s5@example.com")
        create_session(conn, uid, user_agent="Mozilla/5.0", ip="196.25.1.1", now=NOW)
        with conn.cursor() as cur:
            cur.execute("SELECT user_agent, ip FROM user_sessions WHERE user_id = %s",
                        (uid,))
            ua, ip = cur.fetchone()
        assert ua == "Mozilla/5.0"
        assert str(ip) == "196.25.1.1"


class TestExpiry:
    def test_expired_session_is_dead(self, conn):
        uid = _make_user(conn, email="e1@example.com")
        token, _ = create_session(conn, uid, ttl=timedelta(minutes=5), now=NOW)
        assert resolve_session(conn, token, now=NOW + timedelta(minutes=4))
        assert resolve_session(conn, token, now=NOW + timedelta(minutes=6)) is None

    def test_purge_keeps_recent_expiries(self, conn):
        """Recently expired rows stay for forensics; ancient ones go.

        purge_expired sweeps the whole table, so this counts only this
        user's rows — other modules leave sessions behind.
        """
        uid = _make_user(conn, email="e2@example.com")
        create_session(conn, uid, ttl=timedelta(minutes=1), now=NOW)

        def mine() -> int:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM user_sessions WHERE user_id = %s",
                            (uid,))
                return cur.fetchone()[0]

        purge_expired(conn, now=NOW + timedelta(days=2))
        assert mine() == 1, "a session expired 2 days ago is still evidence"
        purge_expired(conn, now=NOW + timedelta(days=40))
        assert mine() == 0


class TestRevocation:
    def test_logout_kills_the_session(self, conn):
        uid = _make_user(conn, email="r1@example.com")
        token, _ = create_session(conn, uid, now=NOW)
        assert revoke_session(conn, token, now=NOW) is True
        assert resolve_session(conn, token, now=NOW) is None

    def test_double_logout_is_harmless(self, conn):
        uid = _make_user(conn, email="r2@example.com")
        token, _ = create_session(conn, uid, now=NOW)
        assert revoke_session(conn, token, now=NOW) is True
        assert revoke_session(conn, token, now=NOW) is False

    def test_revoking_one_session_leaves_the_others(self, conn):
        uid = _make_user(conn, email="r3@example.com")
        laptop, _ = create_session(conn, uid, now=NOW)
        phone, _ = create_session(conn, uid, now=NOW)
        revoke_session(conn, laptop, now=NOW)
        assert resolve_session(conn, laptop, now=NOW) is None
        assert resolve_session(conn, phone, now=NOW) is not None

    def test_revoke_all_logs_out_every_device(self, conn):
        uid = _make_user(conn, email="r4@example.com")
        tokens = [create_session(conn, uid, now=NOW)[0] for _ in range(3)]
        assert revoke_all_for_user(conn, uid, now=NOW) == 3
        assert all(resolve_session(conn, t, now=NOW) is None for t in tokens)

    def test_revoke_all_does_not_touch_other_users(self, conn):
        a = _make_user(conn, email="r5a@example.com")
        b = _make_user(conn, email="r5b@example.com")
        token_b, _ = create_session(conn, b, now=NOW)
        create_session(conn, a, now=NOW)
        revoke_all_for_user(conn, a, now=NOW)
        assert resolve_session(conn, token_b, now=NOW) is not None


class TestDisabledUser:
    def test_disabling_a_user_kills_live_sessions_immediately(self, conn):
        """No revocation sweep required — the join does the work."""
        uid = _make_user(conn, email="d1@example.com")
        token, _ = create_session(conn, uid, now=NOW)
        assert resolve_session(conn, token, now=NOW) is not None
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET disabled_at = %s WHERE id = %s", (NOW, uid))
        assert resolve_session(conn, token, now=NOW) is None

    def test_re_enabling_restores_the_session(self, conn):
        uid = _make_user(conn, email="d2@example.com")
        token, _ = create_session(conn, uid, now=NOW)
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET disabled_at = %s WHERE id = %s", (NOW, uid))
            cur.execute("UPDATE users SET disabled_at = NULL WHERE id = %s", (uid,))
        assert resolve_session(conn, token, now=NOW) is not None


class TestSchema:
    def test_default_role_is_least_privilege(self, conn):
        """A row inserted by the alerts signup funnel must not be an admin."""
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (email) VALUES ('default-role@example.com') "
                "RETURNING role"
            )
            assert cur.fetchone()[0] == "viewer"

    def test_email_is_unique_case_insensitively(self, conn):
        _make_user(conn, email="case@example.com")
        with conn.cursor() as cur, pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute("INSERT INTO users (email) VALUES ('CASE@example.com')")

    def test_sessions_die_with_their_user(self, conn):
        uid = _make_user(conn, email="cascade@example.com")
        create_session(conn, uid, now=NOW)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE id = %s", (uid,))
            cur.execute("SELECT count(*) FROM user_sessions WHERE user_id = %s", (uid,))
            assert cur.fetchone()[0] == 0

    def test_role_enum_rejects_invented_roles(self, conn):
        with conn.cursor() as cur, pytest.raises(psycopg.errors.InvalidTextRepresentation):
            cur.execute(
                "INSERT INTO users (email, role) VALUES ('x@example.com', 'root')"
            )
