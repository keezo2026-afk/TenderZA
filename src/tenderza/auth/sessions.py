"""Opaque server-side sessions (Blueprint §17).

Why not JWTs: a signed token cannot be un-issued. An operator who discovers a
compromised laptop needs revocation to be instant, and "log out" has to mean
logged out. These sessions are a row in `user_sessions`, so revoking is an
UPDATE and every check sees it immediately.

Only the SHA-256 of the token is persisted. The plaintext token exists once,
in the response that creates it; a dump of the database therefore yields no
usable sessions. SHA-256 (not scrypt) is correct here because the token is 256
bits of CSPRNG output — there is no low-entropy secret to slow an attacker down
to, and session checks happen on every request.
"""

from __future__ import annotations

import hashlib
import ipaddress
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

SESSION_COOKIE = "tenderza_session"
DEFAULT_TTL = timedelta(hours=12)
_TOKEN_BYTES = 32


@dataclass(frozen=True)
class Principal:
    """The authenticated caller, as resolved from a session token."""

    user_id: str
    email: str
    role: str
    name: str | None = None
    session_id: str | None = None

    def has_role(self, *roles: str) -> bool:
        return self.role in roles

    @property
    def is_synthetic(self) -> bool:
        """True for principals with no ``users`` row behind them.

        The service token and the auth-disabled dev principal are invented in
        code, not looked up, so their ``user_id`` is not a real key — anything
        that writes to ``users`` or joins on it must check this first.
        """
        return self.session_id is None


def _coerce_ip(value: str | None) -> str | None:
    """Return `value` only if it is a real IP address, else None.

    The `ip` column is `inet`, so junk is a hard error at INSERT time. Clients
    can present anything here — Starlette's TestClient says "testclient", and
    a misconfigured proxy can forward a hostname — and none of that is worth
    failing a login over.
    """
    if not value:
        return None
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return None
    return value


def new_token() -> str:
    """A fresh, URL-safe session token (256 bits of entropy)."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_token(token: str) -> bytes:
    """The stored form of a session token."""
    return hashlib.sha256(token.encode("utf-8")).digest()


def create_session(conn, user_id: str, *, ttl: timedelta = DEFAULT_TTL,
                   user_agent: str | None = None, ip: str | None = None,
                   now: datetime | None = None) -> tuple[str, datetime]:
    """Issue a session. Returns (plaintext_token, expires_at).

    The plaintext is returned to the caller and never stored.
    """
    token = new_token()
    expires_at = (now or _utcnow()) + ttl
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO user_sessions (user_id, token_hash, expires_at,
                                       user_agent, ip)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (user_id, hash_token(token), expires_at, user_agent,
             _coerce_ip(ip)),
        )
    return token, expires_at


def resolve_session(conn, token: str | None, *,
                    now: datetime | None = None) -> Principal | None:
    """Look up the live session behind a token, or None.

    A session is live only when it is unrevoked, unexpired, and its owner is
    not disabled — so disabling a user kills their open sessions too, without
    a separate sweep.
    """
    if not token:
        return None
    from psycopg.rows import dict_row

    moment = now or _utcnow()
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT s.id AS session_id, u.id AS user_id, u.email, u.role, u.name
            FROM user_sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = %s
              AND s.revoked_at IS NULL
              AND s.expires_at > %s
              AND u.disabled_at IS NULL
            """,
            (hash_token(token), moment),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute(
            "UPDATE user_sessions SET last_seen_at = %s WHERE id = %s",
            (moment, row["session_id"]),
        )
    return Principal(
        user_id=str(row["user_id"]),
        email=row["email"],
        role=row["role"],
        name=row["name"],
        session_id=str(row["session_id"]),
    )


def revoke_session(conn, token: str, *, now: datetime | None = None) -> bool:
    """Revoke one session (logout). True when something was revoked."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE user_sessions SET revoked_at = %s "
            "WHERE token_hash = %s AND revoked_at IS NULL",
            (now or _utcnow(), hash_token(token)),
        )
        return cur.rowcount > 0


def revoke_all_for_user(conn, user_id: str, *,
                        now: datetime | None = None) -> int:
    """Revoke every live session for a user (password change, compromise)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE user_sessions SET revoked_at = %s "
            "WHERE user_id = %s AND revoked_at IS NULL",
            (now or _utcnow(), user_id),
        )
        return cur.rowcount


def purge_expired(conn, *, now: datetime | None = None) -> int:
    """Housekeeping: delete sessions that expired over 30 days ago."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM user_sessions WHERE expires_at < %s",
            ((now or _utcnow()) - timedelta(days=30),),
        )
        return cur.rowcount


def _utcnow() -> datetime:
    from datetime import timezone
    return datetime.now(timezone.utc)
