#!/usr/bin/env python3
"""User and role administration (Blueprint §17).

    DATABASE_URL=... python scripts/manage_users.py create ops@example.com --role admin
    DATABASE_URL=... python scripts/manage_users.py list
    DATABASE_URL=... python scripts/manage_users.py set-role a@b.com --role analyst
    DATABASE_URL=... python scripts/manage_users.py passwd a@b.com
    DATABASE_URL=... python scripts/manage_users.py disable a@b.com
    DATABASE_URL=... python scripts/manage_users.py enable a@b.com
    DATABASE_URL=... python scripts/manage_users.py revoke-sessions a@b.com

Passwords are read from a TTY prompt (never echoed), or from the
``TENDERZA_NEW_PASSWORD`` environment variable for scripted provisioning.
They are deliberately NOT accepted as a command-line argument: argv is visible
to every other process on the box via ``ps`` and lands in shell history.

Disabling a user immediately kills their live sessions, because
``resolve_session`` joins on ``disabled_at IS NULL``.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

import psycopg
from psycopg.rows import dict_row

from tenderza import audit
from tenderza.api.auth_routes import MIN_PASSWORD_LEN
from tenderza.auth import hash_password, revoke_all_for_user
from tenderza.auth.deps import ROLE_ORDER

ROLES = tuple(ROLE_ORDER)


def _read_password(confirm: bool = True) -> str:
    env = os.environ.get("TENDERZA_NEW_PASSWORD")
    if env:
        if len(env) < MIN_PASSWORD_LEN:
            raise SystemExit(
                f"TENDERZA_NEW_PASSWORD is shorter than {MIN_PASSWORD_LEN} characters"
            )
        return env
    if not sys.stdin.isatty():
        raise SystemExit(
            "no TTY for a password prompt; set TENDERZA_NEW_PASSWORD instead"
        )
    pw = getpass.getpass("New password: ")
    if len(pw) < MIN_PASSWORD_LEN:
        raise SystemExit(f"password must be at least {MIN_PASSWORD_LEN} characters")
    if confirm and pw != getpass.getpass("Repeat password: "):
        raise SystemExit("passwords do not match")
    return pw


def _find(cur, email: str) -> dict:
    cur.execute(
        "SELECT id, email, role, disabled_at FROM users WHERE lower(email) = %s",
        (email.lower(),),
    )
    row = cur.fetchone()
    if row is None:
        raise SystemExit(f"no such user: {email}")
    return row


def _cli_actor() -> str:
    """Who is at the keyboard. The CLI has no session, so attribution falls
    back to the OS account and host -- weaker than a login, but far better
    than an anonymous privilege grant."""
    import getpass
    import socket
    try:
        return f"cli:{getpass.getuser()}@{socket.gethostname()}"
    except Exception:
        return "cli:unknown"


def cmd_create(conn, args) -> int:
    password = _read_password()
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT id, password_hash FROM users WHERE lower(email) = %s",
                    (args.email.lower(),))
        existing = cur.fetchone()
        if existing and existing["password_hash"]:
            raise SystemExit(f"{args.email} already has a password; use passwd")
        if existing:
            # An alert-only contact is being promoted to a real login.
            cur.execute(
                "UPDATE users SET password_hash = %s, role = %s, name = "
                "coalesce(%s, name) WHERE id = %s",
                (hash_password(password), args.role, args.name, existing["id"]),
            )
            print(f"upgraded existing contact {args.email} to role {args.role}")
        else:
            cur.execute(
                "INSERT INTO users (email, name, password_hash, role) "
                "VALUES (%s, %s, %s, %s)",
                (args.email.lower(), args.name, hash_password(password), args.role),
            )
            print(f"created {args.email} with role {args.role}")
        cur.execute("SELECT id FROM users WHERE lower(email) = %s",
                    (args.email.lower(),))
        new_id = str(cur.fetchone()["id"])
    audit.record(conn, table_name="users", action=audit.USER_CREATED,
                 record_id=new_id, actor=_cli_actor(),
                 detail={"email": args.email.lower(), "role": args.role,
                         "promoted_contact": bool(existing)})
    conn.commit()
    return 0


def cmd_list(conn, args) -> int:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT u.email, u.name, u.role, u.disabled_at, u.last_login_at,
                   u.password_hash IS NOT NULL AS can_login,
                   (SELECT count(*) FROM user_sessions s
                     WHERE s.user_id = u.id AND s.revoked_at IS NULL
                       AND s.expires_at > now()) AS live_sessions
            FROM users u ORDER BY u.role DESC, u.email
            """
        )
        rows = cur.fetchall()
    if not rows:
        print("no users")
        return 0
    print(f"{'EMAIL':<34} {'ROLE':<8} {'LOGIN':<6} {'STATE':<9} {'SESS':>4}  LAST LOGIN")
    for r in rows:
        state = "disabled" if r["disabled_at"] else "active"
        last = r["last_login_at"].strftime("%Y-%m-%d %H:%M") if r["last_login_at"] else "never"
        print(f"{r['email']:<34} {r['role']:<8} "
              f"{'yes' if r['can_login'] else 'no':<6} {state:<9} "
              f"{r['live_sessions']:>4}  {last}")
    return 0


def cmd_set_role(conn, args) -> int:
    with conn.cursor(row_factory=dict_row) as cur:
        user = _find(cur, args.email)
        cur.execute("UPDATE users SET role = %s WHERE id = %s",
                    (args.role, user["id"]))
    audit.record(conn, table_name="users", action=audit.ROLE_CHANGED,
                 record_id=str(user["id"]), actor=_cli_actor(),
                 detail={"email": user["email"], "from": user["role"],
                         "to": args.role})
    conn.commit()
    print(f"{args.email}: role {user['role']} -> {args.role}")
    return 0


def cmd_passwd(conn, args) -> int:
    password = _read_password()
    with conn.cursor(row_factory=dict_row) as cur:
        user = _find(cur, args.email)
        cur.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                    (hash_password(password), user["id"]))
        n = revoke_all_for_user(conn, str(user["id"]))
    audit.record(conn, table_name="users", action=audit.PASSWORD_CHANGED,
                 record_id=str(user["id"]), actor=_cli_actor(),
                 detail={"email": user["email"], "sessions_revoked": n,
                         "by": "cli"})
    conn.commit()
    print(f"{args.email}: password changed, {n} session(s) revoked")
    return 0


def cmd_disable(conn, args) -> int:
    with conn.cursor(row_factory=dict_row) as cur:
        user = _find(cur, args.email)
        cur.execute("UPDATE users SET disabled_at = now() WHERE id = %s",
                    (user["id"],))
        n = revoke_all_for_user(conn, str(user["id"]))
    audit.record(conn, table_name="users", action=audit.USER_DISABLED,
                 record_id=str(user["id"]), actor=_cli_actor(),
                 detail={"email": user["email"], "sessions_revoked": n})
    conn.commit()
    print(f"{args.email}: disabled, {n} session(s) revoked")
    return 0


def cmd_enable(conn, args) -> int:
    with conn.cursor(row_factory=dict_row) as cur:
        user = _find(cur, args.email)
        cur.execute("UPDATE users SET disabled_at = NULL WHERE id = %s",
                    (user["id"],))
    audit.record(conn, table_name="users", action=audit.USER_ENABLED,
                 record_id=str(user["id"]), actor=_cli_actor(),
                 detail={"email": user["email"]})
    conn.commit()
    print(f"{args.email}: enabled")
    return 0


def cmd_revoke_sessions(conn, args) -> int:
    with conn.cursor(row_factory=dict_row) as cur:
        user = _find(cur, args.email)
    n = revoke_all_for_user(conn, str(user["id"]))
    audit.record(conn, table_name="users", action=audit.SESSIONS_REVOKED,
                 record_id=str(user["id"]), actor=_cli_actor(),
                 detail={"email": user["email"], "sessions_revoked": n})
    conn.commit()
    print(f"{args.email}: {n} session(s) revoked")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create", help="create a login (or promote a contact)")
    p.add_argument("email")
    p.add_argument("--role", choices=ROLES, default="viewer")
    p.add_argument("--name")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("list", help="list users, roles and live sessions")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("set-role", help="change a user's role")
    p.add_argument("email")
    p.add_argument("--role", choices=ROLES, required=True)
    p.set_defaults(func=cmd_set_role)

    p = sub.add_parser("passwd", help="set a password (revokes sessions)")
    p.add_argument("email")
    p.set_defaults(func=cmd_passwd)

    p = sub.add_parser("disable", help="revoke access, keep history")
    p.add_argument("email")
    p.set_defaults(func=cmd_disable)

    p = sub.add_parser("enable", help="restore a disabled user")
    p.add_argument("email")
    p.set_defaults(func=cmd_enable)

    p = sub.add_parser("revoke-sessions", help="force logout everywhere")
    p.add_argument("email")
    p.set_defaults(func=cmd_revoke_sessions)

    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2
    with psycopg.connect(dsn) as conn:
        return args.func(conn, args)


if __name__ == "__main__":
    raise SystemExit(main())
