"""
Tests for the password reset flow.

Covers the auth_tokens helpers (signing/verification, expiry, single-use
after password change) and the request/reset HTTP routes (account-existence
non-disclosure, mismatched/short passwords, happy path).
"""

import time

import pytest

from app.auth_tokens import (
    PASSWORD_RESET_TTL_SECONDS,
    make_password_reset_token,
    verify_password_reset_token,
)
from app.models import User

SECRET = "test-secret-key-at-least-32-chars-long"


@pytest.fixture
def user_with_password(app):
    """Set a known password on the test user (id=1)."""
    with app.app_context():
        user = User.get_by_id(1)
        user.set_password("OriginalPwd12345!")
        user.save()
        return user.id


# --- auth_tokens unit tests ------------------------------------------------


class TestTokenLifecycle:
    def test_round_trip(self, app, user_with_password):
        with app.app_context():
            user = User.get_by_id(user_with_password)
            token = make_password_reset_token(SECRET, user)
            recovered = verify_password_reset_token(SECRET, token)
            assert recovered is not None
            assert recovered.id == user.id

    def test_tampered_token_rejected(self, app, user_with_password):
        with app.app_context():
            user = User.get_by_id(user_with_password)
            token = make_password_reset_token(SECRET, user) + "x"
            assert verify_password_reset_token(SECRET, token) is None

    def test_token_signed_with_other_secret_rejected(self, app, user_with_password):
        with app.app_context():
            user = User.get_by_id(user_with_password)
            token = make_password_reset_token("a-different-secret-32-chars-long", user)
            assert verify_password_reset_token(SECRET, token) is None

    def test_token_invalidated_after_password_change(self, app, user_with_password):
        with app.app_context():
            user = User.get_by_id(user_with_password)
            token = make_password_reset_token(SECRET, user)

            # Simulate the user (or someone else) successfully resetting.
            user.set_password("DifferentPwd123!")
            user.save()

            # The old token is now dead — the embedded hash fingerprint no
            # longer matches the user's current password_hash.
            assert verify_password_reset_token(SECRET, token) is None

    def test_token_for_inactive_user_rejected(self, app, user_with_password):
        with app.app_context():
            user = User.get_by_id(user_with_password)
            token = make_password_reset_token(SECRET, user)
            user.is_active = False
            user.save()
            assert verify_password_reset_token(SECRET, token) is None

    def test_expired_token_rejected(self, app, user_with_password, monkeypatch):
        # Patch itsdangerous's clock by stubbing time.time so the token looks
        # ancient on verification.
        with app.app_context():
            user = User.get_by_id(user_with_password)
            token = make_password_reset_token(SECRET, user)

        future_time = time.time() + PASSWORD_RESET_TTL_SECONDS + 60
        monkeypatch.setattr(time, "time", lambda: future_time)

        with app.app_context():
            assert verify_password_reset_token(SECRET, token) is None


# --- HTTP route integration tests ------------------------------------------


class TestForgotPasswordEndpoint:
    def test_unknown_email_returns_same_message(self, client):
        # Account-enumeration protection: the response must not differ between
        # known and unknown emails.
        resp = client.post(
            "/forgot-password",
            data={"email": "nobody@nowhere.example"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"If that email exists" in resp.data

    def test_known_email_returns_same_message(self, client, user_with_password):
        resp = client.post(
            "/forgot-password",
            data={"email": "test@example.com"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"If that email exists" in resp.data

    def test_known_email_logs_reset_url(self, client, user_with_password, caplog):
        import logging

        with caplog.at_level(logging.INFO):
            client.post(
                "/forgot-password",
                data={"email": "test@example.com"},
                follow_redirects=True,
            )

        assert any("/reset-password/" in record.message for record in caplog.records), (
            "Reset URL should be logged for the operator to forward."
        )


class TestResetPasswordEndpoint:
    def test_invalid_token_get_returns_400(self, client):
        resp = client.get("/reset-password/not-a-real-token")
        assert resp.status_code == 400
        assert b"invalid or has expired" in resp.data

    def test_valid_token_get_renders_form(self, client, app, user_with_password):
        with app.app_context():
            user = User.get_by_id(user_with_password)
            token = make_password_reset_token(SECRET, user)
        resp = client.get(f"/reset-password/{token}")
        assert resp.status_code == 200
        assert b'name="password"' in resp.data

    def test_passwords_must_match(self, client, app, user_with_password):
        with app.app_context():
            user = User.get_by_id(user_with_password)
            token = make_password_reset_token(SECRET, user)
        resp = client.post(
            f"/reset-password/{token}",
            data={"password": "NewPassword12!", "password_confirm": "Mismatch12345!"},
        )
        assert resp.status_code == 400
        assert b"don&#39;t match" in resp.data or b"don't match" in resp.data

    def test_short_password_rejected(self, client, app, user_with_password):
        with app.app_context():
            user = User.get_by_id(user_with_password)
            token = make_password_reset_token(SECRET, user)
        resp = client.post(
            f"/reset-password/{token}",
            data={"password": "short", "password_confirm": "short"},
        )
        assert resp.status_code == 400
        assert b"at least 12" in resp.data

    def test_happy_path_updates_password(self, client, app, user_with_password):
        with app.app_context():
            user = User.get_by_id(user_with_password)
            assert user.check_password("OriginalPwd12345!")
            token = make_password_reset_token(SECRET, user)

        resp = client.post(
            f"/reset-password/{token}",
            data={
                "password": "BrandNewSecret123!",
                "password_confirm": "BrandNewSecret123!",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302  # redirect to login

        with app.app_context():
            user = User.get_by_id(user_with_password)
            assert user.check_password("BrandNewSecret123!")
            assert not user.check_password("OriginalPwd12345!")

    def test_token_dead_after_successful_reset(self, client, app, user_with_password):
        with app.app_context():
            user = User.get_by_id(user_with_password)
            token = make_password_reset_token(SECRET, user)

        client.post(
            f"/reset-password/{token}",
            data={
                "password": "BrandNewSecret123!",
                "password_confirm": "BrandNewSecret123!",
            },
        )

        # Try to reuse the same token — must fail.
        resp = client.post(
            f"/reset-password/{token}",
            data={
                "password": "EvenNewer123456!",
                "password_confirm": "EvenNewer123456!",
            },
        )
        assert resp.status_code == 400
        assert b"invalid or has expired" in resp.data
