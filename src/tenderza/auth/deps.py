"""FastAPI auth dependencies — the gate in front of admin routers (§17).

Design rules this module exists to enforce:

* **Deny by default.** `require_role` returns a dependency that fails closed:
  no session, expired session, disabled user, or insufficient role → 401/403.
  A router is protected by declaring the dependency, not by remembering to
  check inside each handler.
* **One source of truth for identity.** Handlers receive a `Principal`; they
  never read cookies or headers themselves, and never trust a client-supplied
  reviewer name (that was the old `reviewer: str = "admin"` hole — anyone
  could sign a human-verified provenance stamp as anyone).
* **Bootstrap without a backdoor.** `TENDERZA_ADMIN_TOKEN` allows cron jobs and
  the first-run operator in via a header. It is opt-in, compared in constant
  time, refuses short values, and is attributed as `service:token` in audit
  records so its actions are never mistaken for a human's.

Role ladder: viewer < analyst < admin.
"""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import Depends, HTTPException, Request

from tenderza.auth.sessions import SESSION_COOKIE, Principal, resolve_session

logger = logging.getLogger(__name__)

ROLE_ORDER = {"viewer": 0, "analyst": 1, "admin": 2}

#: Actions performed with the bootstrap/service token are attributed to this
#: synthetic principal so audit trails stay honest about what was a human.
SERVICE_PRINCIPAL = Principal(
    user_id="00000000-0000-0000-0000-000000000000",
    email="service@tenderza.local",
    role="admin",
    name="service:token",
)

_MIN_TOKEN_LEN = 16

#: Stand-in principal used only when enforcement is switched off for local
#: development. Named so it is unmistakable in any log or audit row.
DEV_PRINCIPAL = Principal(
    user_id="00000000-0000-0000-0000-0000000000de",
    email="dev@localhost",
    role="admin",
    name="auth-disabled",
)


def auth_is_enforced() -> bool:
    """False only when TENDERZA_AUTH is explicitly switched off.

    Fails *safe*: any unrecognised value (including unset) means enforced.
    """
    return os.environ.get("TENDERZA_AUTH", "on").strip().lower() not in {
        "off", "0", "false", "no",
    }


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return None


def _service_token_matches(presented: str | None) -> bool:
    """Constant-time check of the optional bootstrap token."""
    configured = os.environ.get("TENDERZA_ADMIN_TOKEN", "")
    if not configured or not presented:
        return False
    if len(configured) < _MIN_TOKEN_LEN:
        # A short shared secret is worse than none: refuse to honour it rather
        # than let a weak value guard the ops surface.
        return False
    return hmac.compare_digest(configured, presented)


def current_principal(request: Request) -> Principal | None:
    """Resolve the caller, or None when anonymous. Never raises."""
    presented = request.cookies.get(SESSION_COOKIE) or _bearer(request)
    if _service_token_matches(presented):
        return SERVICE_PRINCIPAL
    if not presented:
        return None

    from tenderza.api.app import get_pool
    try:
        pool = get_pool()
    except HTTPException:
        return None
    with pool.connection() as conn:
        principal = resolve_session(conn, presented)
        conn.commit()  # persist last_seen_at
    return principal


def require_role(*roles: str):
    """Dependency factory: allow only these roles (or anything above them).

    `require_role("analyst")` admits analysts *and* admins — seniority is
    implied by the ladder so callers do not have to enumerate it.
    """
    if not roles:
        raise ValueError("require_role needs at least one role")
    floor = min(ROLE_ORDER[r] for r in roles)

    def _dependency(principal: Principal | None = Depends(current_principal)) -> Principal:
        if principal is None and not auth_is_enforced():
            # Local development escape hatch. Logged at WARNING on every use so
            # that a production deployment which mis-set the flag is noisy
            # rather than silently open.
            logger.warning(
                "TENDERZA_AUTH is off — serving a protected route anonymously "
                "as %s. Never run this configuration in production.",
                DEV_PRINCIPAL.name,
            )
            return DEV_PRINCIPAL
        if principal is None:
            # 401 + WWW-Authenticate: the caller may retry with credentials.
            raise HTTPException(
                401, "authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if ROLE_ORDER.get(principal.role, -1) < floor:
            # 403: we know who you are; you may not do this.
            raise HTTPException(
                403, f"role '{principal.role}' may not perform this action",
            )
        return principal

    return _dependency


require_viewer = require_role("viewer")
require_analyst = require_role("analyst")
require_admin = require_role("admin")
