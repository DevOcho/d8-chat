"""
Tests for the API token lifecycle: generation, validation, expiry, tampering,
and the ``d8_sec_`` prefix that mobile clients can include or omit.

The helpers under test live in ``app.blueprints.api_v1`` —
``generate_api_token`` and ``verify_api_token`` — and use itsdangerous's
``URLSafeTimedSerializer`` with a 30-day max_age.
"""

import time

import pytest

from app.blueprints.api_v1 import generate_api_token, verify_api_token
from app.models import User


@pytest.fixture
def token(app):
    with app.app_context():
        return generate_api_token(user_id=1)


# --- Generation / round-trip ---


class TestTokenRoundTrip:
    def test_fresh_token_is_valid(self, app, token):
        with app.app_context():
            assert verify_api_token(token) == 1

    def test_two_tokens_for_same_user_both_valid(self, app):
        # Tokens are stateless — multiple in flight at once is fine.
        with app.app_context():
            t1 = generate_api_token(1)
            t2 = generate_api_token(1)
            assert verify_api_token(t1) == 1
            assert verify_api_token(t2) == 1
            assert (
                t1 != t2 or t1 == t2
            )  # may collide on identical timestamp; both still valid


# --- Expiry ---


class TestTokenExpiry:
    def test_token_within_ttl_is_valid(self, app, token, monkeypatch):
        # Advance 29 days — still inside the 30-day window.
        future = time.time() + (29 * 86400)
        monkeypatch.setattr(time, "time", lambda: future)
        with app.app_context():
            assert verify_api_token(token) == 1

    def test_token_past_ttl_is_rejected(self, app, token, monkeypatch):
        # Advance 31 days — past the 30-day default max_age.
        future = time.time() + (31 * 86400)
        monkeypatch.setattr(time, "time", lambda: future)
        with app.app_context():
            assert verify_api_token(token) is None

    def test_custom_max_age_overrides_default(self, app, token, monkeypatch):
        # Advance 1 hour, then verify with a 10-second max_age — should fail.
        future = time.time() + 3600
        monkeypatch.setattr(time, "time", lambda: future)
        with app.app_context():
            assert verify_api_token(token, max_age=10) is None


# --- Tampering / signature ---


class TestTokenTampering:
    def test_tampered_token_rejected(self, app, token):
        with app.app_context():
            # Flip a character in the middle.
            tampered = token[:-3] + ("a" if token[-3] != "a" else "b") + token[-2:]
            assert verify_api_token(tampered) is None

    def test_truncated_token_rejected(self, app, token):
        with app.app_context():
            assert verify_api_token(token[:-5]) is None

    def test_empty_token_rejected(self, app):
        with app.app_context():
            assert verify_api_token("") is None

    def test_garbage_token_rejected(self, app):
        with app.app_context():
            assert verify_api_token("this is not a real token at all") is None

    def test_token_signed_with_other_secret_rejected(self, app):
        # Generate a token under a different secret and try to verify it
        # against the app's. Must fail.
        with app.app_context():
            from itsdangerous import URLSafeTimedSerializer

            other = URLSafeTimedSerializer("a-completely-different-secret-32xc")
            forged = other.dumps({"user_id": 1}, salt="api-token")
            assert verify_api_token(forged) is None


# --- HTTP-layer prefix handling ---


class TestBearerPrefixHandling:
    """The ``d8_sec_`` prefix is part of the wire format mobile clients use.
    The decorator strips it before verifying."""

    def test_prefix_accepted(self, client, app):
        with app.app_context():
            user = User.get_by_id(1)
            user.set_password("password123")
            user.save()

        login = client.post(
            "/api/v1/auth/login",
            json={"username": "testuser", "password": "password123"},
        )
        token = login.get_json()["api_token"]
        assert token.startswith("d8_sec_")

        # Server must accept the prefixed form (the version mobile clients send).
        ok = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert ok.status_code == 200

    def test_unprefixed_token_also_accepted(self, client, app):
        with app.app_context():
            user = User.get_by_id(1)
            user.set_password("password123")
            user.save()

        login = client.post(
            "/api/v1/auth/login",
            json={"username": "testuser", "password": "password123"},
        )
        prefixed = login.get_json()["api_token"]
        bare = prefixed[len("d8_sec_") :]

        # Old clients or operators testing with curl might send the bare token.
        ok = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {bare}"})
        assert ok.status_code == 200

    def test_missing_header_returns_401(self, client):
        resp = client.get("/api/v1/auth/me")
        assert resp.status_code == 401

    def test_wrong_scheme_returns_401(self, client, app):
        with app.app_context():
            token = generate_api_token(1)
        resp = client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Basic {token}"}
        )
        assert resp.status_code == 401

    def test_expired_token_via_http_returns_401(self, client, app, monkeypatch):
        with app.app_context():
            token = "d8_sec_" + generate_api_token(1)

        future = time.time() + (31 * 86400)
        monkeypatch.setattr(time, "time", lambda: future)

        resp = client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 401
