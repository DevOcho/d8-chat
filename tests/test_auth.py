# tests/test_auth.py

from app.models import User


def test_login_redirect(client):
    """
    GIVEN a Flask application configured for testing
    WHEN the '/chat' page is requested (GET) and the user is not logged in
    THEN check that the response is a redirect to the login page
    """
    response = client.get("/chat", follow_redirects=False)
    assert response.status_code == 302
    assert "/" in response.headers["Location"]


def test_profile_access(logged_in_client):
    """
    WHEN the '/profile' page is requested by a logged-in user
    THEN check for a 200 OK response and that user's data is present.
    """
    response = logged_in_client.get("/profile")
    assert response.status_code == 200
    assert b"Test User" in response.data


def test_logout(logged_in_client):
    """
    WHEN the '/logout' route is requested by a logged-in user
    THEN check that they are logged out and redirected.
    """
    # Hit the logout endpoint
    response = logged_in_client.get("/logout", follow_redirects=True)
    assert response.status_code == 200  # Should land on the index page

    # Now verify that a protected route requires login again
    response = logged_in_client.get("/chat", follow_redirects=False)
    assert response.status_code == 302
    assert "/" in response.headers["Location"]


def test_login_success(client):
    """
    GIVEN a user with a known password
    WHEN they post to /login with valid credentials
    THEN they should be logged in and redirected to /chat
    """
    user = User.get_by_id(1)
    user.set_password("mypassword")
    user.save()

    response = client.post(
        "/login", data={"username": "testuser", "password": "mypassword"}
    )
    assert response.status_code == 302
    assert "/chat" in response.headers["Location"]


def test_login_failure(client):
    """
    GIVEN a user with a known password
    WHEN they post to /login with invalid credentials
    THEN they should be redirected back to the login page with an error
    """
    response = client.post(
        "/login", data={"username": "testuser", "password": "wrongpassword"}
    )
    assert response.status_code == 302
    assert "error=" in response.headers["Location"]


def test_sso_login_redirect(client, mocker):
    """
    WHEN the /sso-login route is hit
    THEN it should redirect to the OAuth provider
    """
    from flask import redirect

    # Prevent Authlib from making real HTTP requests during testing
    mocker.patch(
        "app.blueprints.auth.oauth.authentik.authorize_redirect",
        return_value=redirect("http://fake-sso.com?response_type=code"),
    )

    response = client.get("/sso-login")
    assert response.status_code == 302
    # The URL will be the Authentik authorize URL
    assert "response_type=code" in response.headers["Location"]
