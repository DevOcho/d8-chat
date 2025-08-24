# tests/test_sso.py

from app.models import User, WorkspaceMember, ChannelMember, Channel, Workspace
from flask import request


def test_sso_callback_creates_new_user(client, mocker):
    """
    GIVEN a new user authenticating via SSO for the first time
    WHEN the SSO provider redirects them back to our /auth callback
    THEN a new User, WorkspaceMember, and ChannelMember records should be created.
    """
    # 1. Define the fake data we expect back from the SSO provider
    fake_user_info = {
        "sub": "fake_sso_id_123",
        "email": "new.user@example.com",
        "given_name": "Newbie",
    }

    # 2. Mock the external Authlib calls
    mocker.patch(
        "app.sso.oauth.authentik.authorize_access_token",
        return_value={"access_token": "fake_token"},
    )
    mocker.patch("app.sso.oauth.authentik.parse_id_token", return_value=fake_user_info)

    # 3. Set up the session, since our app expects a 'nonce' to be present
    with client.session_transaction() as sess:
        sess["nonce"] = "test_nonce"

    # 4. Make the request to our callback endpoint
    response = client.get("/auth", follow_redirects=True)

    # --- 5. Assert the results ---

    # Assert we were redirected to the chat page, indicating a successful login
    assert response.status_code == 200
    assert response.request.path == "/chat"

    # Assert a new user was created in the database with the correct details
    new_user = User.get_or_none(User.sso_id == "fake_sso_id_123")
    assert new_user is not None
    assert new_user.email == "new.user@example.com"
    assert new_user.display_name == "Newbie"
    assert new_user.username == "new_user"

    # Assert the user was added to the default workspace
    workspace_member = WorkspaceMember.get_or_none(user=new_user)
    assert workspace_member is not None
    assert workspace_member.role == "member"

    # Assert the user was added to the 'general' and 'announcements' channels
    general_channel = Channel.get(Channel.name == "general")
    announcements_channel = Channel.get(Channel.name == "announcements")

    assert ChannelMember.get_or_none(user=new_user, channel=general_channel) is not None
    assert (
        ChannelMember.get_or_none(user=new_user, channel=announcements_channel)
        is not None
    )


def test_sso_callback_fails_with_incomplete_info(client, mocker):
    """
    Covers: `handle_auth_callback` error path when SSO provider data is missing.
    """
    # Simulate the SSO provider returning data WITHOUT the required 'sub' (subject ID)
    fake_user_info = {"email": "new.user@example.com", "given_name": "Newbie"}
    mocker.patch(
        "app.sso.oauth.authentik.authorize_access_token",
        return_value={"access_token": "fake_token"},
    )
    mocker.patch("app.sso.oauth.authentik.parse_id_token", return_value=fake_user_info)
    with client.session_transaction() as sess:
        sess["nonce"] = "test_nonce"

    response = client.get("/auth", follow_redirects=True)

    # Should redirect back to the login page with an error
    assert response.status_code == 200
    assert response.request.path == "/login"
    # This assertion is brittle, but good enough to check that an error was passed
    assert b"did not return required information" in response.data


def test_sso_callback_links_existing_user(client, mocker):
    """
    Covers: `handle_auth_callback` logic for an existing user logging in via SSO for the first time.
    """
    # 1. Create a user manually, as if they existed before SSO was implemented.
    existing_user = User.create(
        username="existing_user",
        email="existing.user@example.com",
        display_name="Existing User",
        sso_id=None,  # Crucially, they do not have an SSO ID yet
    )

    # 2. Define the fake SSO data that matches the existing user's email
    fake_user_info = {
        "sub": "sso_id_for_existing_user",
        "email": "existing.user@example.com",
        "given_name": "Existing Updated Name",
    }

    # 3. Mock the external Authlib calls
    mocker.patch(
        "app.sso.oauth.authentik.authorize_access_token",
        return_value={"access_token": "fake_token"},
    )
    mocker.patch("app.sso.oauth.authentik.parse_id_token", return_value=fake_user_info)
    with client.session_transaction() as sess:
        sess["nonce"] = "test_nonce"

    # 4. Make the request to our callback endpoint
    client.get("/auth")  # We don't need to check the response here

    # 5. Assert that the existing user record was updated, not a new one created.
    updated_user = User.get(User.email == "existing.user@example.com")
    assert updated_user.id == existing_user.id  # Should be the same user ID
    assert (
        updated_user.sso_id == "sso_id_for_existing_user"
    )  # SSO ID should now be linked
    assert (
        updated_user.display_name == "Existing Updated Name"
    )  # Details should be updated from SSO


def test_sso_new_user_handles_missing_defaults(client, mocker):
    """
    Covers: `handle_auth_callback` warning paths for missing default workspace/channels.
    """
    # First, delete the default workspace to trigger the warning
    Workspace.delete().where(Workspace.name == "DevOcho").execute()

    fake_user_info = {
        "sub": "sso_id_missing_defaults",
        "email": "new.user2@example.com",
        "given_name": "Defaultless",
    }
    mocker.patch(
        "app.sso.oauth.authentik.authorize_access_token",
        return_value={"access_token": "fake_token"},
    )
    mocker.patch("app.sso.oauth.authentik.parse_id_token", return_value=fake_user_info)
    with client.session_transaction() as sess:
        sess["nonce"] = "test_nonce"

    # This should run without error, even though the workspace is missing.
    # The function will print warnings, which is what we are covering.
    response = client.get("/auth", follow_redirects=True)
    assert response.status_code == 200
    assert response.request.path == "/chat"
