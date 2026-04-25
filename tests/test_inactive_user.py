"""
Tests covering the `is_active` session check.

Audit item: a deactivated user with a still-valid session cookie or API token
must not continue working until they log out. Five auth entry points were
hardened (cookie session, Flask-Login user_loader, /ws/chat WS, /ws/api/v1 WS,
api_token_required), plus three login surfaces that need to refuse fresh
auth from deactivated accounts (web /login, /api/v1/auth/login, SSO callback).
"""

from app.models import User


def _set_active(user_id: int, active: bool) -> None:
    user = User.get_by_id(user_id)
    user.is_active = active
    user.save()


# --- Existing-session paths (the user was logged in, then deactivated) ---


def test_cookie_session_drops_when_user_deactivated(logged_in_client):
    """
    GIVEN a user who is already logged in via session cookie
    WHEN their account is deactivated
    THEN protected routes redirect to login as if they had no session.
    """
    # Sanity: protected route works pre-deactivation.
    assert logged_in_client.get("/chat", follow_redirects=False).status_code == 200

    _set_active(1, False)

    response = logged_in_client.get("/chat", follow_redirects=False)
    assert response.status_code == 302
    assert "/" in response.headers["Location"]


def test_api_token_rejected_when_user_deactivated(client):
    """
    GIVEN a user with a working API token
    WHEN their account is deactivated
    THEN subsequent token-authenticated calls return 401.
    """
    user = User.get_by_id(1)
    user.set_password("password123")
    user.save()

    login_res = client.post(
        "/api/v1/auth/login", json={"username": "testuser", "password": "password123"}
    )
    token = login_res.get_json()["api_token"]

    # Token works while active.
    ok = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200

    _set_active(1, False)

    blocked = client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert blocked.status_code == 401
    assert blocked.get_json()["error"] == "User not found"


# --- Fresh-login paths (a deactivated user tries to authenticate from scratch) ---


def test_web_login_refuses_deactivated_user(client):
    """
    A deactivated user submitting valid credentials on the web /login form gets
    the same generic "invalid" error as wrong-password — no account-status leak.
    """
    user = User.get_by_id(1)
    user.set_password("password123")
    user.save()
    _set_active(1, False)

    response = client.post(
        "/login", data={"username": "testuser", "password": "password123"}
    )
    assert response.status_code == 302
    # Same redirect target as the wrong-password case — no leak about active state.
    assert "error=" in response.headers["Location"]
    assert "Invalid" in response.headers["Location"]


def test_api_login_refuses_deactivated_user(client):
    """
    A deactivated user submitting valid credentials to /api/v1/auth/login gets
    the same 401 + "Invalid credentials" message as wrong-password.
    """
    user = User.get_by_id(1)
    user.set_password("password123")
    user.save()
    _set_active(1, False)

    response = client.post(
        "/api/v1/auth/login",
        json={"username": "testuser", "password": "password123"},
    )
    assert response.status_code == 401
    assert response.get_json()["error"] == "Invalid credentials"


# --- get_active_by_id helper itself ---


class TestGetActiveById:
    def test_returns_active_user(self):
        assert User.get_active_by_id(1) is not None

    def test_returns_none_for_missing(self):
        assert User.get_active_by_id(999_999) is None

    def test_returns_none_for_none(self):
        assert User.get_active_by_id(None) is None

    def test_returns_none_for_inactive(self):
        _set_active(1, False)
        assert User.get_active_by_id(1) is None

    def test_returns_user_after_reactivation(self):
        _set_active(1, False)
        assert User.get_active_by_id(1) is None
        _set_active(1, True)
        assert User.get_active_by_id(1) is not None
