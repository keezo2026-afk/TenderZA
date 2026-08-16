"""Authentication and role-based access control (Blueprint §17).

Public surface:

    from tenderza.auth import Principal, require_admin, require_analyst

Password hashing is stdlib scrypt (`passwords`), sessions are opaque
server-side rows (`sessions`), and route protection is dependency-based
(`deps`). No third-party crypto dependency is introduced.
"""

from tenderza.auth.deps import (
    ROLE_ORDER,
    SERVICE_PRINCIPAL,
    current_principal,
    require_admin,
    require_analyst,
    require_role,
    require_viewer,
)
from tenderza.auth.passwords import hash_password, needs_rehash, verify_password
from tenderza.auth.sessions import (
    DEFAULT_TTL,
    SESSION_COOKIE,
    Principal,
    create_session,
    hash_token,
    resolve_session,
    revoke_all_for_user,
    revoke_session,
)

__all__ = [
    "DEFAULT_TTL",
    "ROLE_ORDER",
    "SERVICE_PRINCIPAL",
    "SESSION_COOKIE",
    "Principal",
    "create_session",
    "current_principal",
    "hash_password",
    "hash_token",
    "needs_rehash",
    "require_admin",
    "require_analyst",
    "require_role",
    "require_viewer",
    "resolve_session",
    "revoke_all_for_user",
    "revoke_session",
    "verify_password",
]
