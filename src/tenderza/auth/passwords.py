"""Password hashing (Blueprint §17).

Uses `hashlib.scrypt` from the standard library. That is a deliberate choice:
scrypt is a memory-hard KDF designed for exactly this, it is FIPS-adjacent and
maintained as part of CPython, and it means the auth path adds **no third-party
dependency** — nothing to audit, pin, or have go unmaintained under us.

Stored format is a single self-describing string::

    scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>

The parameters travel with the hash, so raising the cost later does not
invalidate existing passwords: `needs_rehash` flags the stale ones and they are
upgraded transparently on the owner's next successful login.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

# ~100 ms per hash on a modern core. High enough to make offline cracking
# expensive, low enough that a login does not feel slow.
DEFAULT_N = 2 ** 14
DEFAULT_R = 8
DEFAULT_P = 1
_SALT_BYTES = 16
_DK_LEN = 32
_SCHEME = "scrypt"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def hash_password(password: str, *, n: int = DEFAULT_N, r: int = DEFAULT_R,
                  p: int = DEFAULT_P) -> str:
    """Hash a plaintext password into the storable string form."""
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                        dklen=_DK_LEN)
    return f"{_SCHEME}${n}${r}${p}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time check of `password` against a stored hash.

    Returns False (never raises) for absent or malformed hashes, so a user row
    with no password — an alert-only contact — simply cannot log in.
    """
    if not stored or not password:
        return False
    try:
        scheme, n_s, r_s, p_s, salt_s, hash_s = stored.split("$")
        if scheme != _SCHEME:
            return False
        expected = _unb64(hash_s)
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=_unb64(salt_s),
            n=int(n_s), r=int(r_s), p=int(p_s), dklen=len(expected),
        )
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


def needs_rehash(stored: str | None, *, n: int = DEFAULT_N, r: int = DEFAULT_R,
                 p: int = DEFAULT_P) -> bool:
    """True when `stored` was made with weaker parameters than we now want."""
    if not stored:
        return False
    try:
        scheme, n_s, r_s, p_s, _salt, _hash = stored.split("$")
    except ValueError:
        return True
    if scheme != _SCHEME:
        return True
    return (int(n_s), int(r_s), int(p_s)) != (n, r, p)
