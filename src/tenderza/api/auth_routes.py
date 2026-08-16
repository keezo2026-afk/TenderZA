"""Authentication endpoints (Blueprint §17).

POST /auth/login     email + password -> session cookie
POST /auth/logout    revoke the current session
GET  /auth/me        who am I (used by the UI to decide what to render)
POST /auth/password  change own password (revokes other sessions)

Deliberate behaviours:

* **Uniform failure.** Wrong email and wrong password return the identical
  401 "invalid email or password". Distinguishing them tells an attacker which
  addresses are registered — an account-enumeration oracle.
* **Constant-ish work.** A miss still runs a verification against a dummy hash
  so response timing does not leak whether the account exists.
* **Cookie flags.** HttpOnly (JS cannot read the token), SameSite=Lax (blocks
  cross-site POSTs from carrying it, i.e. CSRF), Secure whenever the request
  arrives over HTTPS.
* **Password change re-authenticates and then logs every *other* session out**
  — the standard containment move after a suspected compromise.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from psycopg.rows import dict_row
from pydantic import BaseModel, EmailStr, Field

from tenderza import audit
from tenderza.auth import (
    DEFAULT_TTL,
    SESSION_COOKIE,
    Principal,
    create_session,
    hash_password,
    needs_rehash,
    require_role,
    revoke_all_for_user,
    revoke_session,
    verify_password,
)
from tenderza.auth.deps import auth_is_enforced, current_principal

router = APIRouter(prefix="/auth", tags=["auth"])

# Verified against on a missing account so that "no such user" costs roughly
# the same as "wrong password" (see module docstring).
_DUMMY_HASH = hash_password("timing-equalisation-placeholder")

MIN_PASSWORD_LEN = 12


def get_pool():
    from tenderza.api.app import get_pool as _gp
    return _gp()


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=MIN_PASSWORD_LEN, max_length=1024)


def _set_session_cookie(response: Response, request: Request, token: str,
                        max_age: int) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


@router.post("/login")
def login(req: LoginRequest, request: Request, response: Response,
          pool=Depends(get_pool)):
    email = req.email.lower().strip()
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, email, name, role, password_hash, disabled_at "
                "FROM users WHERE lower(email) = %s",
                (email,),
            )
            user = cur.fetchone()

        # Always do the work, even on a miss, then decide.
        stored = user["password_hash"] if user else None
        ok = verify_password(req.password, stored or _DUMMY_HASH)
        if not user or not stored or not ok or user["disabled_at"] is not None:
            # Audited with the *reason*, which the caller never sees (§17: the
            # 401 stays uniform to avoid an enumeration oracle). Committed on
            # its own, since the request is about to abort.
            reason = ("no_such_user" if not user
                      else "no_password" if not stored
                      else "disabled" if user["disabled_at"] is not None
                      else "bad_password")
            audit.record(
                conn, table_name="users", action=audit.LOGIN_FAIL,
                record_id=str(user["id"]) if user else None,
                actor=email,
                detail={"reason": reason, **audit.request_context(request)},
            )
            conn.commit()
            raise HTTPException(401, "invalid email or password")

        # Opportunistic upgrade: the owner just proved the password, so we can
        # re-hash it at current cost without ever asking them again.
        if needs_rehash(stored):
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                            (hash_password(req.password), user["id"]))

        token, expires_at = create_session(
            conn, str(user["id"]),
            user_agent=request.headers.get("user-agent"),
            ip=request.client.host if request.client else None,
        )
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET last_login_at = now() WHERE id = %s",
                        (user["id"],))
        audit.record(
            conn, table_name="users", action=audit.LOGIN_OK,
            record_id=str(user["id"]), user_id=str(user["id"]),
            actor=user["email"],
            detail={"role": user["role"], **audit.request_context(request)},
        )
        conn.commit()

    _set_session_cookie(response, request, token,
                        max_age=int(DEFAULT_TTL.total_seconds()))
    return {
        "user": {"id": str(user["id"]), "email": user["email"],
                 "name": user["name"], "role": user["role"]},
        "expires_at": expires_at.isoformat(),
        # Returned for non-browser clients (cron, scripts) that cannot hold
        # cookies; browsers should ignore it and rely on the HttpOnly cookie.
        "token": token,
    }


@router.post("/logout")
def logout(request: Request, response: Response, pool=Depends(get_pool)):
    token = request.cookies.get(SESSION_COOKIE)
    revoked = False
    if token:
        with pool.connection() as conn:
            revoked = revoke_session(conn, token)
            if revoked:
                audit.record(conn, table_name="user_sessions",
                             action=audit.LOGOUT,
                             detail=audit.request_context(request))
            conn.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"logged_out": True, "session_revoked": revoked}


@router.get("/me")
def me(principal: Principal | None = Depends(current_principal)):
    """Identity probe. 200 with `authenticated: false` when anonymous —
    the UI polls this to decide whether to show admin nav, and a 401 here
    would be noise in the browser console rather than information."""
    if principal is None:
        return {"authenticated": False, "user": None,
                "auth_required": auth_is_enforced()}
    return {
        "authenticated": True,
        "auth_required": auth_is_enforced(),
        "user": {"id": principal.user_id, "email": principal.email,
                 "name": principal.name, "role": principal.role},
    }


@router.post("/password")
def change_password(req: PasswordChangeRequest, request: Request,
                    response: Response,
                    principal: Principal = Depends(require_role("viewer")),
                    pool=Depends(get_pool)):
    if principal.is_synthetic:
        # The service token and the auth-disabled dev principal have no row in
        # `users`; there is nothing to change and their id is not a real key.
        raise HTTPException(400, "this identity has no password to change")

    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT password_hash FROM users WHERE id = %s",
                        (principal.user_id,))
            row = cur.fetchone()
        if not row or not verify_password(req.current_password,
                                          row["password_hash"]):
            raise HTTPException(401, "current password is incorrect")
        if req.new_password == req.current_password:
            raise HTTPException(422, "new password must differ from the current one")

        with conn.cursor() as cur:
            cur.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                        (hash_password(req.new_password), principal.user_id))
        # Containment: everything else holding this identity is cut loose.
        revoked = revoke_all_for_user(conn, principal.user_id)
        audit.record_for_principal(
            conn, principal, table_name="users",
            action=audit.PASSWORD_CHANGED, record_id=principal.user_id,
            detail={"sessions_revoked": revoked,
                    **audit.request_context(request)},
        )
        token, _ = create_session(
            conn, principal.user_id,
            user_agent=request.headers.get("user-agent"),
            ip=request.client.host if request.client else None,
        )
        conn.commit()

    _set_session_cookie(response, request, token,
                        max_age=int(DEFAULT_TTL.total_seconds()))
    return {"password_changed": True, "other_sessions_revoked": True}
