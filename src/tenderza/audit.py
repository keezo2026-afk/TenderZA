"""Append-only audit trail (Blueprint §17).

The ``audit_logs`` table has existed in the schema since Phase 1 but nothing
wrote to it. Now that the platform has real accounts, roles and privileged
endpoints, the questions an operator will eventually need to answer are:

* who changed this tender's closing date, and when?
* who granted that person admin?
* is somebody grinding passwords against a real account right now?

Design notes
------------

**Append-only by convention.** Nothing in this module updates or deletes. A
log you can quietly edit is not evidence.

**Never break the request.** Auditing is observability, not business logic.
``record()`` swallows and logs its own failures: a full disk or a schema drift
must never turn a successful login into a 500. The one exception is when the
caller passes the connection of an in-flight transaction that later commits --
then the audit row rides along with the change it describes, which is what you
want for anything atomic.

**No secrets in `detail`.** Passwords, hashes and session tokens never go in.
Email addresses do: they are the identity being acted on, and a login-failure
record without the attempted address is useless.

**Synthetic principals write NULL `user_id`.** The service token and the
auth-disabled dev principal have UUID-shaped ids that match no row in
``users``; attribution for them is kept in ``detail.actor`` instead. Same rule
as ``review_queue.reviewed_by``.
"""

from __future__ import annotations

import logging
from typing import Any

from psycopg.types.json import Jsonb

log = logging.getLogger(__name__)

# Actions. Kept as plain strings (the column is text) but centralised so that
# querying the log does not turn into guessing at spellings.
LOGIN_OK = "login.success"
LOGIN_FAIL = "login.failure"
LOGOUT = "logout"
PASSWORD_CHANGED = "password.changed"
ROLE_CHANGED = "role.changed"
USER_CREATED = "user.created"
USER_DISABLED = "user.disabled"
USER_ENABLED = "user.enabled"
SESSIONS_REVOKED = "sessions.revoked"
REVIEW_RESOLVED = "review.resolved"


def record(
    conn,
    *,
    table_name: str,
    action: str,
    record_id: str | None = None,
    user_id: str | None = None,
    actor: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append one audit row. Never raises.

    ``conn`` is an open psycopg connection. The row is written but *not*
    committed -- the caller's commit carries it, so an audited action and its
    audit record land together or not at all.
    """
    payload = dict(detail or {})
    if actor:
        payload.setdefault("actor", actor)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_logs (table_name, record_id, action, user_id, detail)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (table_name, record_id, action, user_id, Jsonb(payload)),
            )
    except Exception:  # pragma: no cover - defensive; see module docstring
        log.warning("audit write failed for action=%s table=%s",
                    action, table_name, exc_info=True)


def record_for_principal(conn, principal, **kwargs) -> None:
    """``record()`` with the actor fields filled in from a Principal.

    Synthetic principals (service token, dev bypass) get a NULL ``user_id``
    because their id is not a foreign key; their label lands in
    ``detail.actor`` so the trail is still readable.
    """
    synthetic = getattr(principal, "is_synthetic", False)
    kwargs.setdefault("user_id", None if synthetic else principal.user_id)
    kwargs.setdefault("actor", principal.email)
    record(conn, **kwargs)


def request_context(request) -> dict[str, Any]:
    """The small, non-sensitive slice of an HTTP request worth keeping.

    The user agent is truncated: some clients send very long strings and the
    audit log should not become a place to stash bulk data.
    """
    if request is None:
        return {}
    ua = request.headers.get("user-agent")
    ctx: dict[str, Any] = {}
    if ua:
        ctx["user_agent"] = ua[:200]
    if request.client and request.client.host:
        # Recorded as text, unlike user_sessions.ip which is an inet column --
        # here a proxy-supplied hostname is still useful evidence.
        ctx["client"] = str(request.client.host)[:100]
    return ctx


def recent(conn, *, limit: int = 100, action: str | None = None,
           email: str | None = None) -> list[dict[str, Any]]:
    """Read back the trail, newest first.

    ``email`` matches either the ``users`` row the entry points at or the
    ``detail.actor`` label, so a single query answers both "what did this
    person do" and "what was done to this account".
    """
    from psycopg.rows import dict_row

    clauses: list[str] = []
    params: list[Any] = []
    if action:
        clauses.append("a.action = %s")
        params.append(action)
    if email:
        clauses.append(
            "(lower(u.email) LIKE %s OR lower(a.detail->>'actor') LIKE %s "
            " OR lower(a.detail->>'email') LIKE %s)"
        )
        needle = f"%{email.lower()}%"
        params.extend([needle, needle, needle])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT a.id, a.at, a.table_name, a.record_id, a.action,
                   a.user_id, a.detail, u.email AS user_email, u.role AS user_role
            FROM audit_logs a
            LEFT JOIN users u ON u.id = a.user_id
            {where}
            ORDER BY a.at DESC, a.id DESC
            LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()

    return [
        {
            "id": r["id"],
            "at": r["at"].isoformat(),
            "action": r["action"],
            "table": r["table_name"],
            "record_id": str(r["record_id"]) if r["record_id"] else None,
            # The actor is the users row when there is one, else the label the
            # writer left behind (cli:..., service:token, dev bypass).
            "actor": r["user_email"] or r["detail"].get("actor"),
            "actor_role": r["user_role"],
            "detail": r["detail"],
        }
        for r in rows
    ]
