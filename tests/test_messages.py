import pytest
import json
from app.models import (
    User,
    Channel,
    ChannelMember,
    Conversation,
    Message,
    UserConversationStatus,
)
from app.routes import PAGE_SIZE


@pytest.fixture
def setup_conversation(test_db):
    """
    A fixture that sets up a common scenario for message tests.
    It creates a second user, a channel, adds both users, and has user1 post a message.
    Returns a dictionary of the created objects.
    """
    # The default 'testuser' (id=1) already exists from conftest.
    user1 = User.get_by_id(1)
    # Create a second user for authorization tests.
    user2 = User.create(id=2, username="anotheruser", email="another@example.com")

    # Create a channel and its corresponding conversation record.
    channel = Channel.create(workspace_id=1, name="test-channel")
    conv, _ = Conversation.get_or_create(
        conversation_id_str=f"channel_{channel.id}", type="channel"
    )

    # Add both users as members of the channel.
    ChannelMember.create(user=user1, channel=channel)
    ChannelMember.create(user=user2, channel=channel)

    # Create the UserConversationStatus record
    UserConversationStatus.create(user=user1, conversation=conv)

    # User 1 posts an initial message.
    message = Message.create(
        user=user1, conversation=conv, content="Original message content"
    )
    return {"user1": user1, "user2": user2, "message": message}


def test_update_message_success(logged_in_client, setup_conversation):
    """
    GIVEN a user who has posted a message
    WHEN the user submits an edit for their own message
    THEN the message should be updated in the database and show "(edited)".
    """
    message = setup_conversation["message"]
    new_content = "This is the edited content."

    response = logged_in_client.put(
        f"/chat/message/{message.id}", data={"content": new_content}
    )

    assert response.status_code == 200
    assert new_content.encode() in response.data
    assert b"(edited)" in response.data

    # Verify the change in the database
    updated_message = Message.get_by_id(message.id)
    assert updated_message.content == new_content
    assert updated_message.is_edited is True


def test_update_message_unauthorized(logged_in_client, setup_conversation):
    """
    GIVEN a message posted by user1
    WHEN user2 tries to edit that message
    THEN they should receive a 403 Forbidden error.
    """
    # Log in as the second user.
    with logged_in_client.session_transaction() as sess:
        sess["user_id"] = 2

    message = setup_conversation["message"]
    original_content = message.content
    response = logged_in_client.put(
        f"/chat/message/{message.id}", data={"content": "unauthorized edit attempt"}
    )

    assert response.status_code == 403

    # Verify the message was NOT changed in the database
    db_message = Message.get_by_id(message.id)
    assert db_message.content == original_content
    assert db_message.is_edited is False


def test_delete_message_success(logged_in_client, setup_conversation):
    """
    GIVEN a user who has posted a message
    WHEN the user deletes their own message
    THEN the message should be removed from the database.
    """
    message = setup_conversation["message"]
    message_id = message.id
    assert Message.get_or_none(id=message_id) is not None

    response = logged_in_client.delete(f"/chat/message/{message_id}")

    assert (
        response.status_code == 204
    )  # 204 No Content is standard for successful DELETE
    assert Message.get_or_none(id=message_id) is None


def test_delete_message_unauthorized(logged_in_client, setup_conversation):
    """
    GIVEN a message posted by user1
    WHEN user2 tries to delete that message
    THEN they should receive a 403 Forbidden error.
    """
    # Log in as the second user.
    with logged_in_client.session_transaction() as sess:
        sess["user_id"] = 2

    message = setup_conversation["message"]
    response = logged_in_client.delete(f"/chat/message/{message.id}")

    assert response.status_code == 403
    assert Message.get_or_none(id=message.id) is not None  # Verify it was not deleted


def test_get_reply_chat_input(logged_in_client, setup_conversation):
    """
    WHEN a user clicks the 'reply' button on a message
    THEN the correct reply-context input form should be returned.
    """
    message = setup_conversation["message"]
    user = setup_conversation["user1"]
    response = logged_in_client.get(f"/chat/message/{message.id}/reply")

    assert response.status_code == 200

    expected_reply_string = f"Replying to {user.display_name}".encode()
    assert expected_reply_string in response.data

    assert b"Original message content" in response.data
    # Check for the hidden input that tracks the parent message
    assert f'name="parent_message_id" value="{message.id}"'.encode() in response.data


def test_load_message_for_edit_success(logged_in_client, setup_conversation):
    """
    GIVEN a message created by the logged-in user
    WHEN the endpoint to load that message for editing is called
    THEN it should return the chat input partial configured for editing.
    """
    message = setup_conversation["message"]
    response = logged_in_client.get(f"/chat/message/{message.id}/load_for_edit")

    assert response.status_code == 200
    assert b"Editing Message" in response.data
    assert (
        b'<textarea id="chat-message-input" name="content" style="display: none;">Original message content</textarea>'
        in response.data
    )


def test_load_message_for_edit_unauthorized(logged_in_client, setup_conversation):
    """
    GIVEN a message created by user1
    WHEN user2 tries to load that message for editing
    THEN they should receive a 403 Forbidden response.
    """
    # Log in as user2
    with logged_in_client.session_transaction() as sess:
        sess["user_id"] = 2

    message = setup_conversation["message"]
    response = logged_in_client.get(f"/chat/message/{message.id}/load_for_edit")

    assert response.status_code == 403


def test_get_older_messages_success(logged_in_client, setup_conversation):
    """
    GIVEN a conversation with more messages than PAGE_SIZE
    WHEN the client requests older messages before the earliest visible one
    THEN it should return a batch of older messages.
    """
    conversation = setup_conversation["message"].conversation
    user = setup_conversation["user1"]

    # Create more messages than one page
    for i in range(PAGE_SIZE):
        Message.create(
            user=user, conversation=conversation, content=f"Older message {i}"
        )

    # The `setup_conversation` already created one message. We need the ID of the
    # first message in our new batch, which will be the one with the lowest ID after the first.
    # We fetch all, sort by ID ascending, and get the second one.
    all_messages = (
        Message.select()
        .where(Message.conversation == conversation)
        .order_by(Message.id)
    )
    cursor_message = all_messages[1]

    response = logged_in_client.get(
        f"/chat/messages/older/{conversation.conversation_id_str}?before_message_id={cursor_message.id}"
    )

    assert response.status_code == 200
    # The response should contain the content of the very first message
    assert b"Original message content" in response.data
    # It should not contain a spinner, because we've reached the beginning
    assert b"spinner-border" not in response.data


def test_get_older_messages_errors(logged_in_client, setup_conversation):
    """
    WHEN the get_older_messages endpoint is called with invalid parameters
    THEN it should return the appropriate error codes.
    """
    conversation = setup_conversation["message"].conversation

    # Case 1: Missing before_message_id
    response_1 = logged_in_client.get(
        f"/chat/messages/older/{conversation.conversation_id_str}"
    )
    assert response_1.status_code == 400

    # Case 2: Non-existent message ID
    response_2 = logged_in_client.get(
        f"/chat/messages/older/{conversation.conversation_id_str}?before_message_id=9999"
    )
    assert response_2.status_code == 404

    # Case 3: Non-existent conversation ID
    response_3 = logged_in_client.get(
        "/chat/messages/older/channel_9999?before_message_id=1"
    )
    assert response_3.status_code == 404


def test_get_message_view(logged_in_client, setup_conversation):
    """
    WHEN a user requests the standard view for a single message
    THEN it should return the message partial.
    """
    message = setup_conversation["message"]
    response = logged_in_client.get(f"/chat/message/{message.id}")

    assert response.status_code == 200
    assert b"message-container" in response.data
    assert b"Original message content" in response.data
    # Ensure it's the display view, not the edit form
    assert b"<form" not in response.data


def test_jump_to_message_unauthorized(logged_in_client, setup_conversation):
    """
    Covers: `jump_to_message` authorization check.
    GIVEN a message in a private channel user2 is not part of
    WHEN user2 tries to jump to that message
    THEN they should receive a 403 Forbidden error.
    """
    message = setup_conversation["message"]
    # Get the channel ID from the conversation string and query for the channel.
    channel_id = int(message.conversation.conversation_id_str.split("_")[1])
    channel = Channel.get_by_id(channel_id)

    # Make the channel private
    channel.is_private = True
    channel.save()

    # Remove user2 from the channel
    user2 = setup_conversation["user2"]
    ChannelMember.delete().where(
        (ChannelMember.user == user2) & (ChannelMember.channel == channel)
    ).execute()

    # Log in as user2 and try to jump
    with logged_in_client.session_transaction() as sess:
        sess["user_id"] = user2.id

    response = logged_in_client.get(f"/chat/message/{message.id}/context")
    assert response.status_code == 403


def test_get_reply_to_nonexistent_message(logged_in_client):
    """
    Covers: `get_reply_chat_input` error path for a message that does not exist.
    """
    response = logged_in_client.get("/chat/message/9999/reply")
    assert response.status_code == 404


def test_get_view_for_nonexistent_message(logged_in_client):
    """
    Covers: `get_message_view` error path for a message that does not exist.
    """
    response = logged_in_client.get("/chat/message/9999")
    assert response.status_code == 404


def test_react_with_invalid_data(logged_in_client, setup_conversation):
    """
    Covers: `toggle_reaction` error path for invalid data.
    """
    message = setup_conversation["message"]
    # Test with no emoji
    response1 = logged_in_client.post(
        f"/chat/message/{message.id}/react", data={"emoji": ""}
    )
    assert response1.status_code == 400

    # Test with a non-existent message id
    response2 = logged_in_client.post("/chat/message/9999/react", data={"emoji": "👍"})
    assert response2.status_code == 400


def test_jump_to_message_in_channel(logged_in_client, setup_conversation):
    """
    Covers: The main success path of `jump_to_message` for a channel message.
    """
    message = setup_conversation["message"]
    # [THE FIX] Get the correct channel directly from the message's conversation,
    # not by randomly selecting the first one from the database.
    channel_id = int(message.conversation.conversation_id_str.split("_")[1])
    channel = Channel.get_by_id(channel_id)

    response = logged_in_client.get(f"/chat/message/{message.id}/context")

    assert response.status_code == 200
    # Check for the HTMX trigger header
    trigger_header = json.loads(response.headers["HX-Trigger"])
    assert trigger_header["jumpToMessage"] == f"#message-{message.id}"

    # Check that the response contains both the correct channel header and the message content
    assert f"#{channel.name}".encode() in response.data
    assert message.content.encode() in response.data


def test_jump_to_message_in_dm(logged_in_client):
    """
    Covers: The main success path of `jump_to_message` for a DM.
    """
    user1 = User.get_by_id(1)
    user2 = User.create(
        id=2, username="dm_partner", email="dm@partner.com", display_name="DM Partner"
    )

    # Setup a DM conversation
    conv, _ = Conversation.get_or_create(
        conversation_id_str=f"dm_{user1.id}_{user2.id}", type="dm"
    )
    UserConversationStatus.create(user=user1, conversation=conv)
    message = Message.create(user=user2, conversation=conv, content="A message in a DM")

    response = logged_in_client.get(f"/chat/message/{message.id}/context")

    assert response.status_code == 200
    # Check for the HTMX trigger header
    trigger_header = json.loads(response.headers["HX-Trigger"])
    assert trigger_header["jumpToMessage"] == f"#message-{message.id}"

    # Check that the response contains the DM partner's name and the message content
    assert b"DM Partner" in response.data
    assert b"A message in a DM" in response.data


def test_jump_to_nonexistent_message(logged_in_client):
    """
    Covers: The 404 error path for `jump_to_message`.
    """
    response = logged_in_client.get("/chat/message/9999/context")
    assert response.status_code == 404
