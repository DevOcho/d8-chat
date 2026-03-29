# tests/test_admin.py
import pytest

from app.models import Channel, ChannelMember, User, Workspace, WorkspaceMember


@pytest.fixture
def admin_client(client, test_db):
    """Creates an admin user and returns a logged-in client for them."""
    admin_user = User.create(
        username="superadmin", email="admin@test.com", display_name="Super Admin"
    )
    admin_user.set_password("password")
    admin_user.save()

    workspace = Workspace.get(name="DevOcho")
    WorkspaceMember.create(user=admin_user, workspace=workspace, role="admin")

    with client.session_transaction() as sess:
        sess["user_id"] = admin_user.id

    return client


def test_admin_required_redirects_non_admin(logged_in_client):
    """
    GIVEN a logged-in regular user
    WHEN they try to access an admin route
    THEN they should be redirected to the chat interface.
    """
    response = logged_in_client.get("/admin/")
    assert response.status_code == 302
    assert "/chat" in response.headers["Location"]


def test_admin_dashboard(admin_client):
    """Test that the admin dashboard loads correctly for an admin."""
    response = admin_client.get("/admin/")
    assert response.status_code == 200
    assert b"Dashboard" in response.data
    assert b"Total Users" in response.data


def test_admin_list_users(admin_client):
    """Test that the user management list loads correctly."""
    response = admin_client.get("/admin/users")
    assert response.status_code == 200
    assert b"User Management" in response.data


def test_admin_create_user(admin_client):
    """Test creating a new user via the admin panel."""
    # 1. Test GET
    res_get = admin_client.get("/admin/users/create")
    assert res_get.status_code == 200

    # 2. Test POST
    res_post = admin_client.post(
        "/admin/users/create",
        data={
            "username": "new_admin_user",
            "email": "new@admin.com",
            "password": "securepassword",
            "role": "admin",
            "display_name": "New Admin",
        },
    )
    assert res_post.status_code == 200
    assert b"created successfully" in res_post.data

    new_user = User.get_or_none(username="new_admin_user")
    assert new_user is not None
    assert new_user.email == "new@admin.com"


def test_admin_edit_user(admin_client):
    """Test editing an existing user via the admin panel."""
    user_to_edit = User.get_by_id(1)  # testuser

    # 1. Test GET
    res_get = admin_client.get(f"/admin/users/edit/{user_to_edit.id}")
    assert res_get.status_code == 200

    # 2. Test POST
    res_post = admin_client.post(
        f"/admin/users/edit/{user_to_edit.id}",
        data={
            "username": "edited_testuser",
            "email": "edited@test.com",
            "role": "member",
        },
    )
    # HTMX endpoints return a 200 with an HX-Redirect header
    assert res_post.status_code == 200
    assert res_post.headers.get("HX-Redirect") == "/admin/users"

    updated_user = User.get_by_id(1)
    assert updated_user.username == "edited_testuser"
    assert updated_user.email == "edited@test.com"


def test_admin_create_duplicate_user(admin_client):
    """Test creating a duplicate user via the admin panel triggers an error."""
    res_post = admin_client.post(
        "/admin/users/create",
        data={
            "username": "testuser",  # Already exists from conftest
            "email": "test@example.com",
            "password": "securepassword",
            "role": "admin",
            "display_name": "Duplicate",
        },
    )
    assert res_post.status_code == 200
    assert b"already exists" in res_post.data


def test_admin_create_channel_invalid(admin_client):
    """Test creating an invalid channel via the admin panel."""
    res_post = admin_client.post(
        "/admin/channels/create",
        data={
            "name": "a",  # Name too short
            "topic": "Admin created topic",
            "description": "Admin created desc",
        },
    )
    assert res_post.status_code == 302  # Redirects back to create channel on error
    assert "/admin/channels/create" in res_post.headers["Location"]


def test_admin_list_channels(admin_client):
    """Test that the channel management list loads correctly."""
    response = admin_client.get("/admin/channels")
    assert response.status_code == 200
    assert b"Channel Management" in response.data


def test_admin_create_channel(admin_client):
    """Test creating a new channel via the admin panel."""
    # 1. Test GET
    res_get = admin_client.get("/admin/channels/create")
    assert res_get.status_code == 200

    # 2. Test POST
    res_post = admin_client.post(
        "/admin/channels/create",
        data={
            "name": "new-admin-channel",
            "topic": "Admin created topic",
            "description": "Admin created desc",
        },
    )
    assert res_post.status_code == 302  # Redirects to channel list

    new_channel = Channel.get_or_none(name="new-admin-channel")
    assert new_channel is not None
    assert new_channel.topic == "Admin created topic"


def test_admin_edit_channel(admin_client):
    """Test editing an existing channel via the admin panel."""
    channel = Channel.get(name="general")

    # 1. Test GET
    res_get = admin_client.get(f"/admin/channels/edit/{channel.id}")
    assert res_get.status_code == 200

    # 2. Test POST
    res_post = admin_client.post(
        f"/admin/channels/edit/{channel.id}",
        data={
            "name": "general",  # Name shouldn't change for defaults
            "topic": "Updated general topic",
            "is_private": "off",
        },
    )
    assert res_post.status_code == 302

    updated_channel = Channel.get_by_id(channel.id)
    assert updated_channel.topic == "Updated general topic"


def test_admin_manage_channel_members(admin_client):
    """Test adding, changing role, and removing channel members via admin."""
    channel = Channel.get(name="general")
    user = User.create(username="chan_user", email="cu@t.com")
    workspace = Workspace.get(name="DevOcho")
    WorkspaceMember.create(user=user, workspace=workspace, role="member")

    # 1. Add Member
    res_add = admin_client.post(
        f"/admin/channels/{channel.id}/members/add", data={"user_id": user.id}
    )
    assert res_add.status_code == 302
    assert ChannelMember.get_or_none(user=user, channel=channel) is not None

    # 2. Update Role
    res_role = admin_client.post(
        f"/admin/channels/{channel.id}/members/{user.id}/role", data={"role": "admin"}
    )
    assert res_role.status_code == 302
    updated_member = ChannelMember.get(user=user, channel=channel)
    assert updated_member.role == "admin"

    # 3. Remove Member
    res_remove = admin_client.post(
        f"/admin/channels/{channel.id}/members/{user.id}/remove"
    )
    assert res_remove.status_code == 302
    assert ChannelMember.get_or_none(user=user, channel=channel) is None
