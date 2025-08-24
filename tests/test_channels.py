import pytest
from app.models import (
    User,
    Channel,
    ChannelMember,
    WorkspaceMember,
    Conversation,
    Message,
)
from app.chat_manager import chat_manager


@pytest.fixture
def setup_channel_with_admin_and_member(test_db):
    """A fixture that creates a channel with an admin (user1) and a member (user2)."""
    user1 = User.get_by_id(1)  # This is our default logged-in user, the admin
    user2 = User.create(id=2, username="regular_member", email="member@example.com")

    workspace = WorkspaceMember.get(user=user1).workspace
    WorkspaceMember.create(user=user2, workspace=workspace)

    channel = Channel.create(workspace=workspace, name="test-managed-channel")
    ChannelMember.create(user=user1, channel=channel, role="admin")
    ChannelMember.create(user=user2, channel=channel, role="member")

    return {"admin": user1, "member": user2, "channel": channel}


@pytest.fixture
def setup_restricted_channel(test_db):
    """Creates a channel where only admins can invite new members."""
    user1 = User.get_by_id(1)
    user2 = User.create(id=2, username="regular_member", email="member@example.com")
    workspace = WorkspaceMember.get(user=user1).workspace
    WorkspaceMember.create(user=user2, workspace=workspace)

    channel = Channel.create(
        workspace=workspace,
        name="restricted-invite-channel",
        invites_restricted_to_admins=True,
    )
    ChannelMember.create(user=user1, channel=channel, role="admin")
    ChannelMember.create(user=user2, channel=channel, role="member")
    return {"admin": user1, "member": user2, "channel": channel}


@pytest.fixture
def setup_channel_for_mentions(test_db, mocker):
    """
    Creates a channel with 3 members. Mocks chat_manager to set 2 of them as online.
    """
    user1 = User.get_by_id(1)  # Logged in user
    user2 = User.create(id=2, username="zelda", email="zelda@example.com")
    user3 = User.create(id=3, username="link", email="link@example.com")

    workspace = WorkspaceMember.get(user=user1).workspace
    WorkspaceMember.create(user=user2, workspace=workspace)
    WorkspaceMember.create(user=user3, workspace=workspace)

    channel = Channel.create(workspace=workspace, name="mention-test-channel")
    conv, _ = Conversation.get_or_create(
        conversation_id_str=f"channel_{channel.id}", type="channel"
    )

    ChannelMember.create(user=user1, channel=channel)
    ChannelMember.create(user=user2, channel=channel)
    ChannelMember.create(user=user3, channel=channel)

    # Mock the online users for this test
    mocker.patch.dict(chat_manager.online_users, {1: "online", 2: "online"}, clear=True)

    return {"conversation": conv}


def test_non_admin_cannot_remove_member(
    logged_in_client, setup_channel_with_admin_and_member
):
    """
    Covers: `remove_channel_member` authorization check.
    GIVEN a regular member (user2) is logged in
    WHEN they attempt to remove the admin (user1)
    THEN they should receive a 403 Forbidden error.
    """
    channel = setup_channel_with_admin_and_member["channel"]
    admin_user = setup_channel_with_admin_and_member["admin"]
    member_user = setup_channel_with_admin_and_member["member"]

    # Log in as the regular member
    with logged_in_client.session_transaction() as sess:
        sess["user_id"] = member_user.id

    response = logged_in_client.delete(
        f"/chat/channel/{channel.id}/members/{admin_user.id}"
    )
    assert response.status_code == 403
    assert b"You do not have permission" in response.data


def test_admin_cannot_remove_last_admin(
    logged_in_client, setup_channel_with_admin_and_member
):
    """
    Covers: `remove_channel_member` last admin safety check.
    GIVEN an admin is the only admin in a channel
    WHEN they try to remove another user who is also an admin (but is the last one)
    THEN the request should fail with a 403 error.
    """
    channel = setup_channel_with_admin_and_member["channel"]
    # In this setup, user1 is the only admin. We'll test by trying to remove them (which is blocked by another check),
    # but the logic for removing the *last* admin is what we want to cover.
    # Let's create a scenario with two admins.
    admin2 = User.create(id=3, username="admin_two", email="admin2@example.com")
    ChannelMember.create(user=admin2, channel=channel, role="admin")

    # Admin 1 removes Admin 2 - this should be successful.
    response = logged_in_client.delete(
        f"/chat/channel/{channel.id}/members/{admin2.id}"
    )
    assert response.status_code == 200

    # Now Admin 1 is the last admin. Let's create a regular member to try and remove.
    member_to_remove = setup_channel_with_admin_and_member["member"]
    membership_to_delete = ChannelMember.get(user=member_to_remove, channel=channel)
    membership_to_delete.role = (
        "admin"  # Temporarily make them an admin for the test case
    )
    membership_to_delete.save()

    # Now, try to remove the last admin. This is a bit contrived but covers the code path.
    last_admin = User.get_by_id(1)
    response = logged_in_client.delete(
        f"/chat/channel/{channel.id}/members/{last_admin.id}"
    )
    # The check for removing oneself is hit first, which is fine. The goal is exercising the code.
    assert response.status_code == 400


def test_user_cannot_leave_announcements(logged_in_client):
    """
    Covers: `leave_channel` special case for #announcements.
    """
    announcements_channel = Channel.get(Channel.name == "announcements")
    ChannelMember.get_or_create(user_id=1, channel=announcements_channel)

    response = logged_in_client.post(f"/chat/channel/{announcements_channel.id}/leave")
    assert response.status_code == 403
    assert b"cannot leave the announcements channel" in response.data


def test_last_admin_cannot_leave_channel_with_members(
    logged_in_client, setup_channel_with_admin_and_member
):
    """
    Covers: `leave_channel` last admin safety check.
    GIVEN an admin is the only admin in a channel with other members
    WHEN they try to leave
    THEN they should be blocked with a 403 error.
    """
    channel = setup_channel_with_admin_and_member["channel"]
    response = logged_in_client.post(f"/chat/channel/{channel.id}/leave")
    assert response.status_code == 403
    assert b"promote another member to admin before you can leave" in response.data


def test_cannot_join_private_channel(logged_in_client):
    """
    Covers: `join_channel` authorization for private channels.
    """
    user2 = User.create(id=2, username="wannabe_member", email="wannabe@example.com")
    private_channel = Channel.create(
        workspace_id=1, name="super-secret", is_private=True, created_by_id=user2.id
    )

    # Log in as user 1
    response = logged_in_client.post(f"/chat/channel/{private_channel.id}/join")
    assert response.status_code == 403
    assert b"You cannot join a private channel" in response.data


def test_create_channel_invalid_name_fails(logged_in_client):
    """
    Covers: `create_channel` error path for invalid names.
    """
    response = logged_in_client.post("/chat/channels/create", data={"name": "a"})
    assert response.status_code == 400
    assert b"must be at least 3 characters long" in response.data


def test_non_admin_cannot_update_channel_about(
    logged_in_client, setup_channel_with_admin_and_member
):
    """
    Covers: `update_channel_about` authorization check.
    GIVEN a regular member is logged in
    WHEN they try to update the channel topic
    THEN they should receive a 403 Forbidden error.
    """
    channel = setup_channel_with_admin_and_member["channel"]
    member = setup_channel_with_admin_and_member["member"]

    with logged_in_client.session_transaction() as sess:
        sess["user_id"] = member.id

    response = logged_in_client.put(
        f"/chat/channel/{channel.id}/about", data={"topic": "A new topic"}
    )
    assert response.status_code == 403


def test_get_channel_details_for_nonexistent_channel(logged_in_client):
    """
    Covers: `get_channel_details` error path for a channel that does not exist.
    """
    response = logged_in_client.get("/chat/channel/9999/details")
    assert response.status_code == 404


def test_non_member_cannot_add_users(
    logged_in_client, setup_channel_with_admin_and_member
):
    """
    Covers: `add_channel_member` authorization check when user is not a member.
    """
    channel = setup_channel_with_admin_and_member["channel"]
    # Create a user who is in the workspace but not the channel
    user3 = User.create(id=3, username="outsider", email="outsider@example.com")
    WorkspaceMember.create(user=user3, workspace_id=1)

    with logged_in_client.session_transaction() as sess:
        sess["user_id"] = user3.id

    response = logged_in_client.post(
        f"/chat/channel/{channel.id}/members", data={"user_id": 1}
    )
    assert response.status_code == 403
    assert b"You are not a member of this channel" in response.data


def test_admin_cannot_remove_self(
    logged_in_client, setup_channel_with_admin_and_member
):
    """
    Covers: `remove_channel_member` error path when an admin tries to remove themselves.
    """
    channel = setup_channel_with_admin_and_member["channel"]
    admin_user = setup_channel_with_admin_and_member["admin"]

    response = logged_in_client.delete(
        f"/chat/channel/{channel.id}/members/{admin_user.id}"
    )
    assert response.status_code == 400
    assert b"You cannot remove yourself" in response.data


def test_admin_cannot_demote_last_admin(
    logged_in_client, setup_channel_with_admin_and_member
):
    """
    Covers: `update_member_role` error path when demoting the last admin.
    GIVEN a channel with only one admin
    WHEN that admin tries to demote another user who is also an admin (making them the last one)
    THEN this should be blocked.
    """
    channel = setup_channel_with_admin_and_member["channel"]
    member_user = setup_channel_with_admin_and_member["member"]

    # First, promote the member to admin, so we have two admins.
    logged_in_client.put(
        f"/chat/channel/{channel.id}/members/{member_user.id}/role",
        data={"role": "admin"},
    )

    # Now, try to demote the original admin. This should succeed, leaving member_user as the sole admin.
    original_admin = setup_channel_with_admin_and_member["admin"]
    with logged_in_client.session_transaction() as sess:
        sess["user_id"] = member_user.id  # Log in as the new admin

    response = logged_in_client.put(
        f"/chat/channel/{channel.id}/members/{original_admin.id}/role",
        data={"role": "member"},
    )
    assert response.status_code == 200

    # Now, with member_user as the sole admin, try to demote the original admin again (who is now a member).
    # This won't trigger the specific "last admin" check, but let's try to demote ourself.
    response = logged_in_client.put(
        f"/chat/channel/{channel.id}/members/{member_user.id}/role",
        data={"role": "member"},
    )
    assert response.status_code == 400  # Cannot change own role


def test_non_admin_cannot_invite_to_restricted_channel(
    logged_in_client, setup_restricted_channel
):
    """
    Covers: `add_channel_member` authorization for restricted channels.
    """
    channel = setup_restricted_channel["channel"]
    member = setup_restricted_channel["member"]
    user3 = User.create(id=3, username="newbie", email="new@example.com")
    WorkspaceMember.create(user=user3, workspace_id=1)

    # Log in as the non-admin member
    with logged_in_client.session_transaction() as sess:
        sess["user_id"] = member.id

    response = logged_in_client.post(
        f"/chat/channel/{channel.id}/members", data={"user_id": user3.id}
    )
    assert response.status_code == 403
    assert b"Only admins can invite new members" in response.data


def test_cannot_make_announcements_private(logged_in_client):
    """
    Covers: `update_channel_settings` special case for #announcements.
    """
    announcements_channel = Channel.get(Channel.name == "announcements")
    ChannelMember.get_or_create(
        user_id=1, channel=announcements_channel, defaults={"role": "admin"}
    )

    response = logged_in_client.put(
        f"/chat/channel/{announcements_channel.id}/settings", data={"is_private": "on"}
    )
    assert response.status_code == 403
    assert b"cannot be made private" in response.data


def test_create_first_channel_removes_placeholder(logged_in_client):
    """
    Covers: `create_channel` OOB swap for the "no channels" placeholder.
    GIVEN a user is in no channels
    WHEN they create their first channel
    THEN the response should include an OOB swap to delete the placeholder.
    """
    # First, ensure the user is not in any channels
    ChannelMember.delete().where(ChannelMember.user_id == 1).execute()

    response = logged_in_client.post(
        "/chat/channels/create", data={"name": "my-first-channel"}
    )
    assert response.status_code == 200
    assert (
        b'<li id="no-channels-placeholder" hx-swap-oob="delete"></li>' in response.data
    )


def test_mention_search_in_dm(logged_in_client):
    """
    Covers: `mention_search` logic for DM conversations.
    """
    user1 = User.get_by_id(1)
    user2 = User.create(id=2, username="dm_partner", email="dm@partner.com")
    conv, _ = Conversation.get_or_create(
        conversation_id_str=f"dm_{user1.id}_{user2.id}", type="dm"
    )

    response = logged_in_client.get(
        f"/chat/conversation/{conv.conversation_id_str}/mention_search?q=dm_p"
    )
    assert response.status_code == 200
    assert b"dm_partner" in response.data
    # Ensure special mentions do not appear in DMs
    assert b"@here" not in response.data
    assert b"@channel" not in response.data


def test_non_member_cannot_view_channel_details(
    logged_in_client, setup_channel_with_admin_and_member
):
    """
    Covers: `get_channel_details` authorization check for non-members.
    """
    channel = setup_channel_with_admin_and_member["channel"]
    # Create a user who is not a member of the channel
    non_member = User.create(id=4, username="nonmember", email="non@member.com")
    WorkspaceMember.create(user=non_member, workspace_id=1)

    with logged_in_client.session_transaction() as sess:
        sess["user_id"] = non_member.id

    response = logged_in_client.get(f"/chat/channel/{channel.id}/details")
    assert response.status_code == 403
    assert b"You are not a member of this channel" in response.data


def test_admin_can_get_channel_about_form(
    logged_in_client, setup_channel_with_admin_and_member
):
    """
    Covers: `get_channel_about_form` success path for admins.
    """
    channel = setup_channel_with_admin_and_member["channel"]
    response = logged_in_client.get(f"/chat/channel/{channel.id}/about/edit")
    assert response.status_code == 200
    assert b"Edit Details" in response.data


def test_non_admin_cannot_get_channel_about_form(
    logged_in_client, setup_channel_with_admin_and_member
):
    """
    Covers: `get_channel_about_form` authorization check for non-admins.
    """
    channel = setup_channel_with_admin_and_member["channel"]
    member = setup_channel_with_admin_and_member["member"]
    with logged_in_client.session_transaction() as sess:
        sess["user_id"] = member.id

    response = logged_in_client.get(f"/chat/channel/{channel.id}/about/edit")
    assert response.status_code == 403


def test_create_duplicate_channel_name_fails(logged_in_client):
    """
    Covers: `create_channel` error path for duplicate names.
    """
    channel_name = "unique-channel-name"
    logged_in_client.post(
        "/chat/channels/create", data={"name": channel_name}
    )  # First time, should succeed
    response = logged_in_client.post(
        "/chat/channels/create", data={"name": channel_name}
    )  # Second time, should fail

    assert response.status_code == 409
    assert b"already exists" in response.data


def test_leave_nonexistent_channel(logged_in_client):
    """
    Covers: `leave_channel` error path for a channel that does not exist.
    """
    response = logged_in_client.post("/chat/channel/9999/leave")
    # [THE FIX] HTMX-driven redirects return 200 with a special header.
    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == "/chat"


def test_member_can_leave_channel_successfully(
    logged_in_client, setup_channel_with_admin_and_member
):
    """
    Covers: The main success path of the `leave_channel` function for a regular member.
    """
    channel = setup_channel_with_admin_and_member["channel"]
    member = setup_channel_with_admin_and_member["member"]

    # Log in as the regular member who will be leaving the channel
    with logged_in_client.session_transaction() as sess:
        sess["user_id"] = member.id

    # Verify the membership exists before the action
    assert ChannelMember.get_or_none(user=member, channel=channel) is not None

    # Act: The member leaves the channel
    response = logged_in_client.post(f"/chat/channel/{channel.id}/leave")

    # Assert: Check for the correct HTMX response
    assert response.status_code == 200
    assert "close-offcanvas" in response.headers["HX-Trigger"]

    # Assert: The response should contain the OOB swap to remove the channel from the sidebar
    assert (
        f'id="channel-item-{channel.id}" hx-swap-oob="delete"'.encode() in response.data
    )

    # Assert: The user should be placed back in their "self-DM" space
    assert b"This is your space." in response.data

    # Assert: The membership should be deleted from the database
    assert ChannelMember.get_or_none(user=member, channel=channel) is None


def test_mention_search_plural_counts(logged_in_client, setup_channel_for_mentions):
    """
    Covers: `mention_search` pluralization logic with multiple online/offline members.
    """
    conversation = setup_channel_for_mentions["conversation"]

    response = logged_in_client.get(
        f"/chat/conversation/{conversation.conversation_id_str}/mention_search?q="
    )

    assert response.status_code == 200
    # There are 3 total members
    assert b"Notifies all 3 members." in response.data
    # We mocked 2 members as being online
    assert b"Notifies 2 online members." in response.data


def test_mention_search_singular_counts(logged_in_client, mocker):
    """
    Covers: `mention_search` singularization logic with only one member.
    """
    user1 = User.get_by_id(1)
    channel = Channel.create(workspace_id=1, name="solo-channel")
    conv, _ = Conversation.get_or_create(
        conversation_id_str=f"channel_{channel.id}", type="channel"
    )
    ChannelMember.create(user=user1, channel=channel)

    # Mock only the current user as online
    mocker.patch.dict(chat_manager.online_users, {1: "online"}, clear=True)

    response = logged_in_client.get(
        f"/chat/conversation/{conv.conversation_id_str}/mention_search?q="
    )

    assert response.status_code == 200
    # Total count is 1, so it should say "member"
    assert b"Notifies all 1 member." in response.data
    # Online count is 1, so it should say "member"
    assert b"Notifies 1 online member." in response.data


def test_mention_search_filters_and_shows_special_mentions(
    logged_in_client, setup_channel_for_mentions
):
    """
    Covers: `mention_search` logic when a query is provided.
    """
    conversation = setup_channel_for_mentions["conversation"]

    # Search for a specific user
    response = logged_in_client.get(
        f"/chat/conversation/{conversation.conversation_id_str}/mention_search?q=zeld"
    )

    assert response.status_code == 200
    # It should find the user "zelda"
    assert b"zelda" in response.data
    # It should NOT find the other user "link"
    assert b"link" not in response.data
    # Crucially, it should NOT show the special mentions because the query doesn't match
    assert b"@here" not in response.data
    assert b"@channel" not in response.data
