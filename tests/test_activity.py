# tests/test_activity.py

import pytest
import datetime
from app.models import (
    User,
    Channel,
    ChannelMember,
    Conversation,
    Message,
    UserConversationStatus,
)


@pytest.fixture
def setup_threads(test_db):
    """Sets up a user, a channel, and a threaded conversation for testing."""
    user1 = User.get_by_id(1)
    user2 = User.create(id=2, username="user_two", email="two@example.com")
    channel = Channel.get(name="general")
    ChannelMember.create(user=user2, channel=channel)
    conv = Conversation.get(conversation_id_str=f"channel_{channel.id}")

    # A parent message from user2
    parent_msg = Message.create(user=user2, conversation=conv, content="Parent message")

    # A reply from user1 (the logged-in user)
    Message.create(
        user=user1,
        conversation=conv,
        content="My reply in the thread",
        parent_message=parent_msg,
        reply_type="thread",
    )

    # A newer reply from user2 that should make the thread "unread"
    reply2 = Message.create(
        user=user2,
        conversation=conv,
        content="A newer reply",
        parent_message=parent_msg,
        reply_type="thread",
    )
    parent_msg.last_reply_at = reply2.created_at
    parent_msg.save()

    return {"user1": user1, "user2": user2, "parent_message": parent_msg}


def test_view_all_threads_marks_as_read(logged_in_client, setup_threads):
    """
    GIVEN a user with an unread thread
    WHEN they view the /chat/threads page
    THEN their last_threads_view_at timestamp should be updated.
    """
    user1 = setup_threads["user1"]
    one_day_ago = datetime.datetime.now() - datetime.timedelta(days=1)
    user1.last_threads_view_at = one_day_ago
    user1.save()

    # Sanity check: ensure the timestamp is in the past
    assert User.get_by_id(1).last_threads_view_at < datetime.datetime.now()

    # Act: View the threads page
    logged_in_client.get("/chat/threads")

    # Assert: The user's timestamp should be updated to now
    updated_user = User.get_by_id(1)
    assert updated_user.last_threads_view_at > one_day_ago
    # Check that the response contains the thread content
    response = logged_in_client.get("/chat/threads")
    assert b"Parent message" in response.data
    assert b"A newer reply" not in response.data  # Replies are not in the list view
    assert b"2 replies" in response.data


def test_view_all_unreads_clears_badges(logged_in_client):
    """
    GIVEN a user with an unread DM
    WHEN they view the /chat/unreads page
    THEN the response should contain the unread message AND OOB swaps to clear the badges.
    """
    # Arrange
    user1 = User.get_by_id(1)
    user2 = User.create(id=2, username="dm_sender", email="sender@example.com")
    conv, _ = Conversation.get_or_create(
        conversation_id_str=f"dm_{user1.id}_{user2.id}", type="dm"
    )
    status, _ = UserConversationStatus.get_or_create(user=user1, conversation=conv)
    status.last_read_timestamp = datetime.datetime.now() - datetime.timedelta(hours=1)
    status.save()
    Message.create(user=user2, conversation=conv, content="Unread DM for you")

    # Act
    response = logged_in_client.get("/chat/unreads")
    response_data = response.data

    # Assert
    assert response.status_code == 200
    assert b"Unread DM for you" in response_data

    # 1. Check that the OOB swap for the DM's badge is present.
    assert (
        f'id="unread-badge-{conv.conversation_id_str}" hx-swap-oob="outerHTML"'.encode()
        in response_data
    )

    # 2. Check that the OOB swap to un-bold the specific DM link is present.
    assert (
        f'hx-swap-oob="outerHTML:#link-{conv.conversation_id_str}"'.encode()
        in response_data
    )

    # 3. Check that the OOB swap for the main "Unreads" link is present and has the correct "read" class.
    assert b'id="unreads-link"' in response_data
    assert b'hx-swap-oob="true"' in response_data
    assert b'class="text-decoration-none text-light text-opacity-75"' in response_data
