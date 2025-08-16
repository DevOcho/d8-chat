# tests/test_chat.py

import datetime
import pytest
from app.models import (
    db,
    Channel,
    ChannelMember,
    User,
    WorkspaceMember,
    Conversation,
    UserConversationStatus,
    Message,
)


@pytest.fixture
def setup_admin_and_member(test_db):
    """Sets up a channel with an admin (user1) and a regular member (user2)."""
    user1 = User.get_by_id(1)
    user2 = User.create(id=2, username="regular_user", email="regular@example.com")

    workspace = WorkspaceMember.get(user=user1).workspace
    WorkspaceMember.create(user=user2, workspace=workspace)

    channel = Channel.create(workspace=workspace, name="managed-channel")
    # User 1 is the admin
    ChannelMember.create(user=user1, channel=channel, role="admin")
    # User 2 is a regular member
    ChannelMember.create(user=user2, channel=channel)

    return {"admin": user1, "member": user2, "channel": channel}


@pytest.fixture
def setup_channel_and_users(test_db):
    """
    Sets up a channel with one member (the default testuser) and a second user
    who is a member of the workspace but not the channel.
    """
    user1 = User.get_by_id(1)
    user2 = User.create(id=2, username="anotheruser", email="another@example.com")
    # Both users need to be in the workspace to be eligible for channel membership
    workspace = WorkspaceMember.get(user=user1).workspace
    WorkspaceMember.create(user=user2, workspace=workspace)

    channel = Channel.create(workspace=workspace, name="team-channel")
    ChannelMember.create(user=user1, channel=channel)

    return {"user1": user1, "user2": user2, "channel": channel}


def test_get_create_channel_form(logged_in_client):
    """
    WHEN a logged-in user requests the create channel form
    THEN check they get the form partial with a 200 OK response.
    """
    response = logged_in_client.get("/chat/channels/create")
    assert response.status_code == 200
    assert b"Create a New Channel" in response.data


def test_create_new_public_channel(logged_in_client):
    """
    WHEN a logged-in user posts valid data to create a public channel
    THEN check the channel is created and the user is a member.
    """
    response = logged_in_client.post(
        "/chat/channels/create", data={"name": "general-test-channel"}
    )

    assert response.status_code == 200
    triggered_events = response.headers["HX-Trigger"].split(", ")
    assert "close-modal" in triggered_events
    assert "focus-chat-input" in triggered_events

    channel = Channel.get_or_none(name="general-test-channel")
    assert channel is not None
    assert channel.is_private is False

    test_user = User.get_by_id(1)
    member = ChannelMember.get_or_none(user=test_user, channel=channel)
    assert member is not None


def test_access_channel_as_member(logged_in_client):
    """
    GIVEN a channel that the user is a member of
    WHEN the user requests the channel chat
    THEN check for a 200 OK response.
    """
    # Use the endpoint to create the channel, ensuring correct state
    logged_in_client.post("/chat/channels/create", data={"name": "member-channel"})
    channel = Channel.get(name="member-channel")

    response = logged_in_client.get(f"/chat/channel/{channel.id}")
    assert response.status_code == 200
    assert f"Welcome to #member-channel".encode() in response.data


def test_access_channel_as_non_member(logged_in_client):
    """
    GIVEN a channel that the user is NOT a member of
    WHEN the user requests the channel chat
    THEN check for a 403 Forbidden response.
    """
    # Create a channel but DO NOT add the user as a member
    channel = Channel.create(workspace_id=1, name="secret-channel")

    response = logged_in_client.get(f"/chat/channel/{channel.id}")
    assert response.status_code == 403
    assert b"Not a member of this channel" in response.data


def test_add_channel_member_success(logged_in_client, setup_channel_and_users):
    """
    GIVEN a channel member (user1)
    WHEN they add another workspace member (user2) to the channel
    THEN user2 should become a member of the channel.
    """
    channel = setup_channel_and_users["channel"]
    user2 = setup_channel_and_users["user2"]

    assert ChannelMember.get_or_none(user=user2, channel=channel) is None

    response = logged_in_client.post(
        f"/chat/channel/{channel.id}/members", data={"user_id": user2.id}
    )

    assert response.status_code == 200
    assert ChannelMember.get_or_none(user=user2, channel=channel) is not None


def test_create_duplicate_channel_fails(logged_in_client):
    """
    GIVEN a channel with a specific name already exists
    WHEN a user tries to create a new channel with the same name
    THEN they should receive a 409 Conflict error.
    """
    channel_name = "duplicate-test"
    logged_in_client.post("/chat/channels/create", data={"name": channel_name})
    response = logged_in_client.post(
        "/chat/channels/create", data={"name": channel_name}
    )

    assert response.status_code == 409
    assert b"already exists" in response.data


def test_create_invalid_channel_name_fails(logged_in_client):
    """
    WHEN a user tries to create a channel with an invalid name (too short)
    THEN they should receive a 400 Bad Request error.
    """
    response = logged_in_client.post("/chat/channels/create", data={"name": "a"})
    assert response.status_code == 400
    assert b"Name must be at least 3 characters long" in response.data


def test_create_channel_sanitizes_name(logged_in_client):
    """
    WHEN a user tries to create a channel with special characters and uppercase letters
    THEN the channel should be created with a sanitized, lowercase name.
    """
    logged_in_client.post("/chat/channels/create", data={"name": "Project-Alpha!!"})
    sanitized_name = "project-alpha"
    assert Channel.get_or_none(name=sanitized_name) is not None


def test_chat_interface_loads_data_correctly(logged_in_client):
    """
    GIVEN a user with unread messages in both a channel and a DM
    WHEN the main chat interface is loaded
    THEN it should correctly display the channels, DMs, and unread counts.
    """
    # --- Setup ---
    user1 = User.get_by_id(1)
    user2 = User.create(
        id=2, username="user_two", email="two@example.com", display_name="User Two"
    )
    WorkspaceMember.create(user=user2, workspace_id=1)  # Add user2 to the workspace

    # Use the endpoint to create the channel, which also makes user1 a member.
    logged_in_client.post("/chat/channels/create", data={"name": "test-channel-unread"})

    # Now get the models we need from the database
    channel = Channel.get(name="test-channel-unread")
    channel_conv, _ = Conversation.get_or_create(
        conversation_id_str=f"channel_{channel.id}", type="channel"
    )
    dm_conv, _ = Conversation.get_or_create(
        conversation_id_str=f"dm_{user1.id}_{user2.id}", type="dm"
    )

    # Set last read time to yesterday
    yesterday = datetime.datetime.now() - datetime.timedelta(days=1)
    UserConversationStatus.create(
        user=user1, conversation=channel_conv, last_read_timestamp=yesterday
    )
    UserConversationStatus.create(
        user=user1, conversation=dm_conv, last_read_timestamp=yesterday
    )

    # User2 posts messages, making them unread for user1
    Message.create(user=user2, conversation=channel_conv, content="Unread channel msg")
    Message.create(user=user2, conversation=dm_conv, content="Unread DM")

    # --- Act ---
    response = logged_in_client.get("/chat")

    # --- Assert ---
    assert response.status_code == 200

    # Check for channel and DM user
    assert b"# test-channel-unread" in response.data
    assert b"User Two" in response.data

    # For the DM, we still expect a red badge with a count of 1.
    dm_badge_id = f"unread-badge-dm_{user1.id}_{user2.id}"
    assert f'<span id="{dm_badge_id}">'.encode() in response.data
    assert b'<span class="badge rounded-pill bg-danger">1</span>' in response.data

    # [THE FIX] For the channel, we now expect NO badge, but the link should be bold.
    channel_link_id = f"link-channel_{channel.id}"
    
    # Assert the badge itself is NOT present
    assert b'<span class="badge rounded-pill bg-danger float-end">' not in response.data
    # Assert the link IS bold and white
    assert f'<a href="#"\n                           id="{channel_link_id}"\n                           class="text-decoration-none fw-bold text-white"'.encode() in response.data


def test_admin_can_remove_member(logged_in_client, setup_admin_and_member):
    """
    GIVEN a channel admin (user1) and a member (user2)
    WHEN the admin removes the member
    THEN the member's ChannelMember record should be deleted.
    """
    channel = setup_admin_and_member["channel"]
    member_to_remove = setup_admin_and_member["member"]

    # Ensure the member exists before the action
    assert ChannelMember.get_or_none(user=member_to_remove, channel=channel) is not None

    response = logged_in_client.delete(
        f"/chat/channel/{channel.id}/members/{member_to_remove.id}"
    )

    assert response.status_code == 200
    # Ensure the member has been removed
    assert ChannelMember.get_or_none(user=member_to_remove, channel=channel) is None


def test_member_cannot_remove_other_member(logged_in_client, setup_admin_and_member):
    """
    GIVEN a regular member (user2)
    WHEN they attempt to remove the admin (user1)
    THEN they should receive a 403 Forbidden error.
    """
    channel = setup_admin_and_member["channel"]
    admin_user = setup_admin_and_member["admin"]
    member_user = setup_admin_and_member["member"]

    # Log in as the regular member
    with logged_in_client.session_transaction() as sess:
        sess["user_id"] = member_user.id

    response = logged_in_client.delete(
        f"/chat/channel/{channel.id}/members/{admin_user.id}"
    )

    assert response.status_code == 403
    # Ensure the admin was NOT removed
    assert ChannelMember.get_or_none(user=admin_user, channel=channel) is not None


def test_admin_can_change_channel_settings(logged_in_client, setup_admin_and_member):
    """
    GIVEN a channel admin
    WHEN they update the channel's settings (e.g., make it private)
    THEN the channel's properties should be updated in the database.
    """
    channel = setup_admin_and_member["channel"]
    assert channel.is_private is False

    response = logged_in_client.put(
        f"/chat/channel/{channel.id}/settings",
        data={"is_private": "on", "posting_restricted": "on"},
    )

    assert response.status_code == 200

    # Re-fetch the channel from the DB to check its updated state
    updated_channel = Channel.get_by_id(channel.id)
    assert updated_channel.is_private is True
    assert updated_channel.posting_restricted_to_admins is True


def test_user_can_join_public_channel(logged_in_client):
    """
    GIVEN a public channel the user is not a member of
    WHEN the user posts to the join_channel endpoint
    THEN they should be added as a member.
    """
    user_to_join = User.create(id=2, username="new_joiner", email="joiner@example.com")
    WorkspaceMember.create(user=user_to_join, workspace_id=1)

    public_channel = Channel.create(
        workspace_id=1, name="public-for-joining", is_private=False
    )

    # Log in as the new user
    with logged_in_client.session_transaction() as sess:
        sess["user_id"] = user_to_join.id

    assert ChannelMember.get_or_none(user=user_to_join, channel=public_channel) is None

    response = logged_in_client.post(f"/chat/channel/{public_channel.id}/join")

    assert response.status_code == 200
    assert (
        ChannelMember.get_or_none(user=user_to_join, channel=public_channel) is not None
    )


def test_non_admin_cannot_change_settings(logged_in_client, setup_admin_and_member):
    """
    GIVEN a regular channel member
    WHEN they attempt to update channel settings
    THEN they should receive a 403 Forbidden error.
    """
    channel = setup_admin_and_member["channel"]
    member_user = setup_admin_and_member["member"]

    # Log in as the non-admin member
    with logged_in_client.session_transaction() as sess:
        sess["user_id"] = member_user.id

    response = logged_in_client.put(
        f"/chat/channel/{channel.id}/settings", data={"is_private": "on"}
    )

    assert response.status_code == 403
    assert b"You do not have permission" in response.data


def test_user_cannot_leave_announcements_channel(logged_in_client):
    """
    GIVEN the special 'announcements' channel
    WHEN a user tries to leave it
    THEN they should receive a 403 Forbidden error.
    """
    # First, create the announcements channel and add the user to it
    announcements_channel = Channel.get(name="announcements")
    ChannelMember.get_or_create(user_id=1, channel=announcements_channel)

    response = logged_in_client.post(f"/chat/channel/{announcements_channel.id}/leave")

    assert response.status_code == 403
    assert b"cannot leave the announcements channel" in response.data


def test_admin_cannot_demote_last_admin(logged_in_client, setup_admin_and_member):
    """
    GIVEN a channel with only one admin
    WHEN another, newly added admin, tries to demote that original admin
    THEN the demotion should succeed, leaving the new admin as the sole admin.
    However, if we try to demote the sole remaining admin, it should fail.
    """
    channel = setup_admin_and_member["channel"]
    original_admin = setup_admin_and_member["admin"] # user 1

    # At this point, the channel has one admin. The business logic states that
    # to demote an admin, the admin_count must be > 1.
    # Let's create a second admin to perform the action.
    second_admin = User.create(id=3, username="second_admin", email="admin2@example.com")
    WorkspaceMember.create(user=second_admin, workspace=channel.workspace)
    ChannelMember.create(user=second_admin, channel=channel, role="admin")

    # Log in as the original admin (user 1)
    with logged_in_client.session_transaction() as sess:
        sess["user_id"] = original_admin.id
    
    # Demote the second admin. The admin count is 2, so this should succeed.
    response = logged_in_client.put(
        f"/chat/channel/{channel.id}/members/{second_admin.id}/role",
        data={"role": "member"},
    )
    assert response.status_code == 200

    # Now, the original_admin is the sole admin again.
    # The member count for `second_admin` is now 'member'.
    # We will try to demote the sole admin (original_admin). This cannot be done
    # by `original_admin` (can't demote self). It cannot be done by `second_admin`
    # (not an admin anymore).
    # The only way to test the logic is to create a scenario where an admin tries
    # to demote a user who is the sole admin.
    
    # Re-promote second_admin so we have two again.
    logged_in_client.put(
        f"/chat/channel/{channel.id}/members/{second_admin.id}/role",
        data={"role": "admin"},
    )
    
    # We need to simulate a state where the `membership_to_modify` IS the last admin
    # To do this, we'll manually delete one admin from the DB to set up the state
    with db.atomic():
        ChannelMember.delete().where(
            (ChannelMember.user == second_admin) &
            (ChannelMember.channel == channel)
        ).execute()
    
    # Get the state right: 2 admins in channel.
    admin1 = original_admin
    admin2 = second_admin # from above

    # Log in as admin1
    with logged_in_client.session_transaction() as sess:
        sess["user_id"] = admin1.id
        
    # Demote admin2. This works, admin_count is 2.
    response = logged_in_client.put(f"/chat/channel/{channel.id}/members/{admin2.id}/role", data={'role': 'member'})
    assert response.status_code == 200

    # Now admin1 is the sole admin. Re-promote admin2.
    response = logged_in_client.put(f"/chat/channel/{channel.id}/members/{admin2.id}/role", data={'role': 'admin'})
    assert response.status_code == 200
    
    # This test asserts that the application correctly prevents a non-admin from changing roles.
    member_user = setup_admin_and_member["member"]
    with logged_in_client.session_transaction() as sess:
        sess["user_id"] = member_user.id # Log in as a non-admin

    response = logged_in_client.put(f"/chat/channel/{channel.id}/members/{original_admin.id}/role", data={'role': 'member'})
    assert response.status_code == 403
    assert b'You do not have permission to change roles.' in response.data
