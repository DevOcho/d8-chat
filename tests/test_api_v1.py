# tests/test_api_v1.py

from app.models import User


def test_api_login_success(client):
    """
    GIVEN a user with a known password
    WHEN the API login endpoint is called with valid credentials
    THEN it should return a 200 response with an api_token and user data
    """
    # The 'testuser' (id=1) is created by conftest.py, but lacks a password. Let's set one.
    user = User.get_by_id(1)
    user.set_password("password123")
    user.save()

    response = client.post(
        "/api/v1/auth/login", json={"username": "testuser", "password": "password123"}
    )

    assert response.status_code == 200
    data = response.get_json()

    assert "api_token" in data
    assert data["api_token"].startswith("d8_sec_")
    assert "user" in data
    assert data["user"]["username"] == "testuser"


def test_api_login_failure(client):
    """
    WHEN the API login endpoint is called with invalid credentials
    THEN it should return a 401 Unauthorized response
    """
    response = client.post(
        "/api/v1/auth/login", json={"username": "testuser", "password": "wrongpassword"}
    )
    assert response.status_code == 401
    assert response.get_json()["error"] == "Invalid credentials"


def test_api_get_me_success(client):
    """
    GIVEN a valid api_token
    WHEN the /api/v1/auth/me endpoint is called with the token in the Authorization header
    THEN it should return the authenticated user's details
    """
    user = User.get_by_id(1)
    user.set_password("password123")
    user.save()

    # Login to get the token
    login_res = client.post(
        "/api/v1/auth/login", json={"username": "testuser", "password": "password123"}
    )
    token = login_res.get_json()["api_token"]

    # Use the token to access the protected route
    me_res = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert me_res.status_code == 200
    data = me_res.get_json()
    assert data["user"]["username"] == "testuser"


def test_api_get_me_unauthorized(client):
    """
    WHEN the /api/v1/auth/me endpoint is called without a token
    THEN it should return a 401 Unauthorized response
    """
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.get_json()["error"] == "Missing or invalid token"


def test_api_get_workspaces_success(client):
    """
    GIVEN a valid api_token
    WHEN the /api/v1/workspaces endpoint is called
    THEN it should return a list of workspaces the user is in
    """
    user = User.get_by_id(1)
    user.set_password("password123")
    user.save()

    login_res = client.post(
        "/api/v1/auth/login", json={"username": "testuser", "password": "password123"}
    )
    token = login_res.get_json()["api_token"]

    res = client.get("/api/v1/workspaces", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    data = res.get_json()

    assert "workspaces" in data
    assert len(data["workspaces"]) > 0
    assert data["workspaces"][0]["name"] == "DevOcho"  # Default from conftest


def test_api_get_channels_success(client):
    """
    GIVEN a valid api_token
    WHEN the /api/v1/channels endpoint is called
    THEN it should return the channels the user is a member of
    """
    from app.models import Channel, ChannelMember

    user = User.get_by_id(1)
    user.set_password("password123")
    user.save()

    # Explicitly add the testuser to the general channel so the list isn't empty
    channel = Channel.get(Channel.name == "general")
    ChannelMember.get_or_create(user=user, channel=channel)

    login_res = client.post(
        "/api/v1/auth/login", json={"username": "testuser", "password": "password123"}
    )
    token = login_res.get_json()["api_token"]

    res = client.get("/api/v1/channels", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    data = res.get_json()

    assert "channels" in data
    assert len(data["channels"]) > 0

    # Check that the unread counts are present in the response
    first_channel = data["channels"][0]
    assert "unread_count" in first_channel
    assert "mention_count" in first_channel


def test_api_get_dms_success(client):
    """
    GIVEN a valid api_token
    WHEN the /api/v1/dms endpoint is called
    THEN it should return the active DMs for the user
    """
    user = User.get_by_id(1)
    user.set_password("password123")
    user.save()

    login_res = client.post(
        "/api/v1/auth/login", json={"username": "testuser", "password": "password123"}
    )
    token = login_res.get_json()["api_token"]

    res = client.get("/api/v1/dms", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    data = res.get_json()

    assert "dms" in data
    # By default in conftest, testuser doesn't have any active DMs initialized
    assert isinstance(data["dms"], list)
