"""Role-ladder and service-token tests (§17) — no DB, no HTTP."""

import dataclasses

import pytest
from fastapi import HTTPException

from tenderza.auth.deps import (
    ROLE_ORDER,
    SERVICE_PRINCIPAL,
    _service_token_matches,
    auth_is_enforced,
    require_role,
)
from tenderza.auth.sessions import Principal


def _principal(role: str) -> Principal:
    return Principal(user_id="u1", email=f"{role}@example.com", role=role)


class TestRoleLadder:
    def test_ladder_is_ordered(self):
        assert ROLE_ORDER["viewer"] < ROLE_ORDER["analyst"] < ROLE_ORDER["admin"]

    @pytest.mark.parametrize("required,role,allowed", [
        ("viewer",  "viewer",  True),
        ("viewer",  "analyst", True),
        ("viewer",  "admin",   True),
        ("analyst", "viewer",  False),
        ("analyst", "analyst", True),
        ("analyst", "admin",   True),   # seniority is implied
        ("admin",   "viewer",  False),
        ("admin",   "analyst", False),
        ("admin",   "admin",   True),
    ])
    def test_seniority_is_implied(self, required, role, allowed):
        dep = require_role(required)
        if allowed:
            assert dep(_principal(role)).role == role
        else:
            with pytest.raises(HTTPException) as exc:
                dep(_principal(role))
            assert exc.value.status_code == 403

    def test_anonymous_is_401_with_a_challenge(self):
        """401 (not 403) so a client knows to authenticate and retry."""
        with pytest.raises(HTTPException) as exc:
            require_role("viewer")(None)
        assert exc.value.status_code == 401
        assert exc.value.headers["WWW-Authenticate"] == "Bearer"

    def test_unknown_role_is_denied_not_promoted(self):
        """A role string we do not recognise must fail closed."""
        rogue = Principal(user_id="u", email="e", role="superuser")
        with pytest.raises(HTTPException) as exc:
            require_role("viewer")(rogue)
        assert exc.value.status_code == 403

    def test_require_role_needs_a_role(self):
        with pytest.raises(ValueError):
            require_role()


class TestServiceToken:
    def test_absent_configuration_denies_everything(self, monkeypatch):
        monkeypatch.delenv("TENDERZA_ADMIN_TOKEN", raising=False)
        assert _service_token_matches("anything") is False
        assert _service_token_matches(None) is False

    def test_matching_token_is_accepted(self, monkeypatch):
        monkeypatch.setenv("TENDERZA_ADMIN_TOKEN", "a-sufficiently-long-token")
        assert _service_token_matches("a-sufficiently-long-token") is True

    def test_wrong_token_rejected(self, monkeypatch):
        monkeypatch.setenv("TENDERZA_ADMIN_TOKEN", "a-sufficiently-long-token")
        assert _service_token_matches("a-sufficiently-long-tokeN") is False

    def test_short_token_refused_even_when_it_matches(self, monkeypatch):
        """A weak shared secret guarding /ops is worse than no shortcut."""
        monkeypatch.setenv("TENDERZA_ADMIN_TOKEN", "short")
        assert _service_token_matches("short") is False

    def test_service_principal_is_identifiable_in_audit(self):
        assert SERVICE_PRINCIPAL.name == "service:token"
        assert SERVICE_PRINCIPAL.role == "admin"


class TestEnforcementFlag:
    def test_enforced_by_default(self, monkeypatch):
        monkeypatch.delenv("TENDERZA_AUTH", raising=False)
        assert auth_is_enforced() is True

    @pytest.mark.parametrize("value", ["off", "OFF", "0", "false", "no", " off "])
    def test_explicit_off_values(self, monkeypatch, value):
        monkeypatch.setenv("TENDERZA_AUTH", value)
        assert auth_is_enforced() is False

    @pytest.mark.parametrize("value", ["on", "", "yes", "1", "enforce", "maybe"])
    def test_anything_else_fails_safe(self, monkeypatch, value):
        """A typo in the flag must leave auth ON, never off."""
        monkeypatch.setenv("TENDERZA_AUTH", value)
        assert auth_is_enforced() is True

    def test_dev_bypass_only_applies_when_disabled(self, monkeypatch):
        monkeypatch.setenv("TENDERZA_AUTH", "off")
        principal = require_role("admin")(None)
        assert principal.role == "admin"
        assert principal.name == "auth-disabled"  # unmistakable in logs

    def test_dev_bypass_does_not_apply_when_enforced(self, monkeypatch):
        monkeypatch.setenv("TENDERZA_AUTH", "on")
        with pytest.raises(HTTPException) as exc:
            require_role("admin")(None)
        assert exc.value.status_code == 401


class TestPrincipal:
    def test_has_role(self):
        assert _principal("admin").has_role("admin", "analyst")
        assert not _principal("viewer").has_role("admin")

    def test_principal_is_immutable(self):
        """Frozen so a handler cannot escalate itself mid-request."""
        with pytest.raises(dataclasses.FrozenInstanceError):
            _principal("viewer").role = "admin"


class TestDemoAdminSeed:
    """The demo admin must be able to log in through the real endpoint.

    Regression: the default was once `admin@tenderza.local`, which pydantic's
    EmailStr rejects (RFC 6762 reserves `.local` as special-use). The account
    seeded fine and then bounced off /auth/login's own validator with a 422 --
    a one-command demo that cannot reach its own admin pages.
    """

    def _demo_defaults(self):
        import ast
        import pathlib

        src = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "dev_demo.py"
        tree = ast.parse(src.read_text())
        found: dict[str, str] = {}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and getattr(node.func, "attr", None) == "add_argument"):
                continue
            flags = [a.value for a in node.args if isinstance(a, ast.Constant)]
            default = next((kw.value.value for kw in node.keywords
                            if kw.arg == "default"
                            and isinstance(kw.value, ast.Constant)), None)
            for flag in flags:
                if flag in ("--demo-admin", "--demo-password"):
                    found[flag] = default
        return found

    def test_demo_admin_email_passes_the_login_validator(self):
        from tenderza.api.auth_routes import LoginRequest

        defaults = self._demo_defaults()
        email = defaults["--demo-admin"]
        # Must not raise: this is the exact model /auth/login validates against.
        req = LoginRequest(email=email, password="x" * 12)
        assert req.email == email
        assert not email.endswith(".local"), "reserved TLD; EmailStr rejects it"

    def test_demo_password_meets_the_minimum_length(self):
        from tenderza.api.auth_routes import MIN_PASSWORD_LEN

        password = self._demo_defaults()["--demo-password"]
        assert len(password) >= MIN_PASSWORD_LEN, (
            "the demo password must be changeable via /auth/password"
        )
