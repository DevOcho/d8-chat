# tests/test_chat.py

import datetime
import pytest
from app.models import (
    Channel,
    ChannelMember,
    User,
    WorkspaceMember,
    Conversation,
    UserConversationStatus,
    Message,
)


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
    assert response.headers["HX-Trigger"] == "close-modal"

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

    # CORRECTED: Check for channel with the space
    assert b"# test-channel-unread" in response.data
    # Check for DM user
    assert b"User Two" in response.data

    # CORRECTED: Check for the DM unread badge with a less brittle assertion
    dm_badge_id = f"unread-badge-dm_{user1.id}_{user2.id}"
    assert f'<span id="{dm_badge_id}">'.encode() in response.data
    assert b'<span class="badge rounded-pill bg-danger">1</span>' in response.data

    # Check for channel unread badge
    channel_badge_id = f"unread-badge-channel_{channel.id}"
    assert f'<span id="{channel_badge_id}">'.encode() in response.data
    assert (
        b'<span class="badge rounded-pill bg-danger float-end">1</span>'
        in response.data
    )
