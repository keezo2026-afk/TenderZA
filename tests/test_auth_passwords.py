"""Password hashing tests (§17) — pure functions, no DB."""

import pytest

from tenderza.auth.passwords import (
    DEFAULT_N,
    hash_password,
    needs_rehash,
    verify_password,
)

# Cheap parameters so the suite stays fast; production cost is DEFAULT_N.
FAST = {"n": 2 ** 4, "r": 8, "p": 1}


class TestHashing:
    def test_roundtrip(self):
        stored = hash_password("correct horse battery staple", **FAST)
        assert verify_password("correct horse battery staple", stored)

    def test_wrong_password_rejected(self):
        stored = hash_password("s3cret-passphrase", **FAST)
        assert not verify_password("s3cret-passphras", stored)
        assert not verify_password("", stored)

    def test_hash_is_salted(self):
        """Identical passwords must not produce identical hashes, or a
        rainbow table cracks every reused password at once."""
        a = hash_password("same-password-here", **FAST)
        b = hash_password("same-password-here", **FAST)
        assert a != b
        assert verify_password("same-password-here", a)
        assert verify_password("same-password-here", b)

    def test_plaintext_never_appears_in_the_stored_form(self):
        stored = hash_password("hunter2-hunter2", **FAST)
        assert "hunter2" not in stored

    def test_format_is_self_describing(self):
        stored = hash_password("another-passphrase", **FAST)
        scheme, n, r, p, salt, digest = stored.split("$")
        assert scheme == "scrypt"
        assert (int(n), int(r), int(p)) == (FAST["n"], FAST["r"], FAST["p"])
        assert salt and digest

    def test_empty_password_refused(self):
        with pytest.raises(ValueError):
            hash_password("")


class TestVerifyIsTotal:
    """verify_password must never raise — it sits on the login path."""

    @pytest.mark.parametrize("stored", [
        None, "", "not-a-hash", "scrypt$broken", "bcrypt$1$2$3$4$5",
        "scrypt$notanint$8$1$c2FsdA==$aGFzaA==", "$$$$$",
    ])
    def test_malformed_stored_hash_is_false_not_an_exception(self, stored):
        assert verify_password("anything", stored) is False

    def test_user_with_no_password_cannot_log_in(self):
        """Alert-only contacts have password_hash NULL (§14)."""
        assert verify_password("guess", None) is False


class TestRehashPolicy:
    def test_current_parameters_need_no_rehash(self):
        stored = hash_password("passphrase-current")
        assert needs_rehash(stored) is False

    def test_weaker_parameters_are_flagged(self):
        stored = hash_password("passphrase-weak", **FAST)
        assert needs_rehash(stored) is True

    def test_unknown_scheme_is_flagged(self):
        assert needs_rehash("bcrypt$2b$12$abcdef") is True

    def test_absent_hash_is_not_flagged(self):
        # Nothing to upgrade; the user simply cannot log in.
        assert needs_rehash(None) is False

    def test_default_cost_is_not_accidentally_lowered(self):
        """A guard against someone 'optimising' the KDF into uselessness."""
        assert DEFAULT_N >= 2 ** 14
