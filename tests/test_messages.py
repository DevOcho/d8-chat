# tests/test_messages.py

import json
from datetime import datetime, timedelta

import pytest

from app.models import (
    Channel,
    ChannelMember,
    Conversation,
    Message,
    User,
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


@pytest.fixture
def setup_dm_conversation(test_db):
    """
    Fixture that creates a direct message conversation. The DM
    initial-load / jump path renders ``dm_messages.html`` and parses the
    ``dm_{a}_{b}`` id, so the newer-fetch and jump flows need dedicated
    coverage for this conversation type.
    """
    user1 = User.get_by_id(1)
    user2 = User.create(
        id=2, username="dm_partner", email="dm@partner.com", display_name="DM Partner"
    )
    conv, _ = Conversation.get_or_create(
        conversation_id_str=f"dm_{user1.id}_{user2.id}", type="dm"
    )
    UserConversationStatus.create(user=user1, conversation=conv)
    message = Message.create(
        user=user2, conversation=conv, content="DM original message"
    )
    return {"user1": user1, "user2": user2, "conversation": conv, "message": message}


def _seed_context(conversation, user, n_before, n_after):
    """
    Seed ``n_before`` messages, a single ``TARGET MESSAGE``, then ``n_after``
    messages, all with strictly increasing ``created_at`` (and therefore ids).
    Returns the target message. Used to exercise the jump-to-message context
    window and its ``has_older`` / ``has_newer`` loader flags.
    """
    base = datetime(2026, 7, 8, 8, 0, 0)
    for i in range(n_before):
        Message.create(
            user=user,
            conversation=conversation,
            content=f"before {i}",
            created_at=base + timedelta(minutes=i),
        )
    target = Message.create(
        user=user,
        conversation=conversation,
        content="TARGET MESSAGE",
        created_at=base + timedelta(minutes=n_before),
    )
    for j in range(n_after):
        Message.create(
            user=user,
            conversation=conversation,
            content=f"after {j}",
            created_at=base + timedelta(minutes=n_before + 1 + j),
        )
    return target


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
    all_messages = list(
        Message.select()
        .where(Message.conversation == conversation)
        .order_by(Message.id)
    )
    cursor_message = all_messages[1]

    response = logged_in_client.get(
        f"/chat/messages/{conversation.conversation_id_str}?before_message_id={cursor_message.id}"
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
        f"/chat/messages/{conversation.conversation_id_str}"
    )
    assert response_1.status_code == 400

    # Case 2: Non-existent message ID
    response_2 = logged_in_client.get(
        f"/chat/messages/{conversation.conversation_id_str}?before_message_id=9999"
    )
    assert response_2.status_code == 404

    # Case 3: Non-existent conversation ID
    response_3 = logged_in_client.get("/chat/messages/channel_9999?before_message_id=1")
    assert response_3.status_code == 404


def test_get_newer_messages_success(logged_in_client, setup_conversation):
    """
    GIVEN a cursor message with several newer messages after it
    WHEN the client requests newer messages via `after_message_id`
    THEN the batch should return those messages in chronological order.
    """
    conversation = setup_conversation["message"].conversation
    user = setup_conversation["user1"]

    base = datetime(2026, 7, 8, 8, 0, 0)
    cursor = Message.create(
        user=user, conversation=conversation, content="cursor msg", created_at=base
    )
    for i in range(3):
        Message.create(
            user=user,
            conversation=conversation,
            content=f"newer body {i}",
            created_at=base + timedelta(minutes=i + 1),
        )

    response = logged_in_client.get(
        f"/chat/messages/{conversation.conversation_id_str}?after_message_id={cursor.id}"
    )

    assert response.status_code == 200
    # All newer messages present, in ascending order.
    idx0 = response.data.index(b"newer body 0")
    idx1 = response.data.index(b"newer body 1")
    idx2 = response.data.index(b"newer body 2")
    assert idx0 < idx1 < idx2
    # The cursor message itself must not be included (strict inequality).
    assert b"cursor msg" not in response.data
    # A partial page (< PAGE_SIZE) renders no newer sentinel.
    assert b"newer-message-loader" not in response.data


def test_newer_batch_adds_separator_at_midnight_seam(
    logged_in_client, setup_conversation
):
    """
    GIVEN the cursor already on screen sits late on one day and the newer batch
          begins the next day
    WHEN the client requests the newer batch
    THEN exactly one date separator is rendered at the day boundary.
    """
    conversation = setup_conversation["message"].conversation
    user = setup_conversation["user1"]

    midnight = (datetime.now() + timedelta(days=2)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    cursor = Message.create(
        user=user,
        conversation=conversation,
        content="just before midnight",
        created_at=midnight - timedelta(minutes=2),
    )
    Message.create(
        user=user,
        conversation=conversation,
        content="just after midnight",
        created_at=midnight + timedelta(minutes=3),
    )
    Message.create(
        user=user,
        conversation=conversation,
        content="later that day",
        created_at=midnight + timedelta(hours=9),
    )

    response = logged_in_client.get(
        f"/chat/messages/{conversation.conversation_id_str}?after_message_id={cursor.id}"
    )

    assert response.status_code == 200
    assert response.data.count(b"date-separator") == 1


def test_newer_batch_separator_precedes_new_day_message(
    logged_in_client, setup_conversation
):
    """
    GIVEN a newer batch that crosses a day boundary mid-batch
    WHEN the batch is rendered
    THEN the single date separator appears between the last day-1 message and
         the first day-2 message.
    """
    conversation = setup_conversation["message"].conversation
    user = setup_conversation["user1"]

    midnight = (datetime.now() + timedelta(days=2)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    cursor = Message.create(
        user=user,
        conversation=conversation,
        content="cursor day one",
        created_at=midnight - timedelta(hours=1),
    )
    Message.create(
        user=user,
        conversation=conversation,
        content="still day one",
        created_at=midnight - timedelta(minutes=30),
    )
    Message.create(
        user=user,
        conversation=conversation,
        content="now day two",
        created_at=midnight + timedelta(minutes=30),
    )

    response = logged_in_client.get(
        f"/chat/messages/{conversation.conversation_id_str}?after_message_id={cursor.id}"
    )

    assert response.status_code == 200
    assert response.data.count(b"date-separator") == 1
    sep_idx = response.data.index(b"date-separator")
    assert response.data.index(b"still day one") < sep_idx
    assert sep_idx < response.data.index(b"now day two")


def test_newer_pagination_chains_to_next_page(logged_in_client, setup_conversation):
    """
    GIVEN more than PAGE_SIZE newer messages
    WHEN the first newer batch is fetched and then its sentinel URL is followed
    THEN the sentinel points at the last message of the batch and the second
         page returns the remainder with no further sentinel.
    """
    conversation = setup_conversation["message"].conversation
    user = setup_conversation["user1"]

    base = datetime(2026, 7, 8, 8, 0, 0)
    cursor = Message.create(
        user=user, conversation=conversation, content="cursor msg", created_at=base
    )
    ids = []
    for i in range(PAGE_SIZE + 5):
        m = Message.create(
            user=user,
            conversation=conversation,
            content=f"chain {i}",
            created_at=base + timedelta(minutes=i + 1),
        )
        ids.append(m.id)

    first = logged_in_client.get(
        f"/chat/messages/{conversation.conversation_id_str}?after_message_id={cursor.id}"
    )
    assert first.status_code == 200
    # The sentinel must chain from the last message of the returned page.
    last_id_in_page = ids[PAGE_SIZE - 1]
    assert f"after_message_id={last_id_in_page}".encode() in first.data

    second = logged_in_client.get(
        f"/chat/messages/{conversation.conversation_id_str}"
        f"?after_message_id={last_id_in_page}"
    )
    assert second.status_code == 200
    # Remaining 5 messages, and no further sentinel.
    assert b"chain 34" in second.data
    assert b"newer-message-loader" not in second.data


def test_newer_batch_excludes_thread_replies(logged_in_client, setup_conversation):
    """
    GIVEN a mix of a normal message and a thread reply after the cursor
    WHEN the newer batch is rendered
    THEN the thread reply is omitted (threads live in the thread view) while the
         normal message is shown.
    """
    conversation = setup_conversation["message"].conversation
    user = setup_conversation["user1"]

    base = datetime(2026, 7, 8, 8, 0, 0)
    cursor = Message.create(
        user=user, conversation=conversation, content="cursor msg", created_at=base
    )
    Message.create(
        user=user,
        conversation=conversation,
        content="normal newer body",
        created_at=base + timedelta(minutes=1),
    )
    Message.create(
        user=user,
        conversation=conversation,
        content="threaded newer body",
        parent_message=cursor,
        reply_type="thread",
        created_at=base + timedelta(minutes=2),
    )

    response = logged_in_client.get(
        f"/chat/messages/{conversation.conversation_id_str}?after_message_id={cursor.id}"
    )

    assert response.status_code == 200
    assert b"normal newer body" in response.data
    assert b"threaded newer body" not in response.data


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
    # Get the correct channel directly from the message's conversation,
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


def test_jump_channel_renders_both_loaders(logged_in_client, setup_conversation):
    """
    GIVEN a channel message with a full page of history on both sides
    WHEN a member jumps to it
    THEN both the older and newer sentinels render (has_older/has_newer true).
    """
    conversation = setup_conversation["message"].conversation
    user = setup_conversation["user1"]
    target = _seed_context(
        conversation, user, n_before=PAGE_SIZE + 1, n_after=PAGE_SIZE + 1
    )

    response = logged_in_client.get(f"/chat/message/{target.id}/context")

    assert response.status_code == 200
    assert b"older-message-loader" in response.data
    assert b"newer-message-loader" in response.data
    assert json.loads(response.headers["HX-Trigger"])["jumpToMessage"] == (
        f"#message-{target.id}"
    )


def test_jump_channel_newest_edge_hides_newer_loader(
    logged_in_client, setup_conversation
):
    """
    GIVEN a target near the newest edge (fewer than a page of newer messages)
    WHEN a member jumps to it
    THEN only the older sentinel renders (has_newer false).
    """
    conversation = setup_conversation["message"].conversation
    user = setup_conversation["user1"]
    target = _seed_context(conversation, user, n_before=PAGE_SIZE + 1, n_after=5)

    response = logged_in_client.get(f"/chat/message/{target.id}/context")

    assert response.status_code == 200
    assert b"older-message-loader" in response.data
    assert b"newer-message-loader" not in response.data


def test_jump_channel_oldest_edge_hides_older_loader(
    logged_in_client, setup_conversation
):
    """
    GIVEN a target near the oldest edge (fewer than a page of older messages)
    WHEN a member jumps to it
    THEN only the newer sentinel renders (has_older false).
    """
    conversation = setup_conversation["message"].conversation
    user = setup_conversation["user1"]
    target = _seed_context(conversation, user, n_before=0, n_after=PAGE_SIZE + 1)

    response = logged_in_client.get(f"/chat/message/{target.id}/context")

    assert response.status_code == 200
    assert b"older-message-loader" not in response.data
    assert b"newer-message-loader" in response.data


def test_jump_dm_renders_both_loaders(logged_in_client, setup_dm_conversation):
    """
    GIVEN a DM message with a full page of history on both sides
    WHEN the participant jumps to it
    THEN both older and newer sentinels render in the DM template too.
    """
    conversation = setup_dm_conversation["conversation"]
    user = setup_dm_conversation["user2"]
    target = _seed_context(
        conversation, user, n_before=PAGE_SIZE + 1, n_after=PAGE_SIZE + 1
    )

    response = logged_in_client.get(f"/chat/message/{target.id}/context")

    assert response.status_code == 200
    assert b"older-message-loader" in response.data
    assert b"newer-message-loader" in response.data
    assert json.loads(response.headers["HX-Trigger"])["jumpToMessage"] == (
        f"#message-{target.id}"
    )


def test_load_for_thread_reply(logged_in_client, setup_conversation):
    """
    WHEN a user clicks to reply to a message within a thread view
    THEN it should load the thread-specific reply HTML
    """
    parent_message = setup_conversation["message"]
    # User 2 replies in thread
    thread_reply = Message.create(
        user=setup_conversation["user2"],
        conversation=parent_message.conversation,
        content="Thread reply",
        parent_message=parent_message,
        reply_type="thread",
    )

    response = logged_in_client.get(
        f"/chat/message/{thread_reply.id}/load_for_thread_reply"
    )
    assert response.status_code == 200
    assert b"Replying to " in response.data
    assert f'id="thread-input-container-{parent_message.id}"'.encode() in response.data


def test_get_thread_chat_input(logged_in_client, setup_conversation):
    """
    WHEN a thread is opened
    THEN it should load the clean thread-specific input form
    """
    parent_message = setup_conversation["message"]
    response = logged_in_client.get(f"/chat/input/thread/{parent_message.id}")
    assert response.status_code == 200
    assert f'id="thread-input-container-{parent_message.id}"'.encode() in response.data


def test_load_message_for_thread_edit(logged_in_client, setup_conversation):
    """
    WHEN a user clicks to edit their own message in a thread
    THEN it should load the thread-specific edit HTML
    """
    parent_message = setup_conversation["message"]

    # User 1 replies to themselves in a thread
    thread_reply = Message.create(
        user=setup_conversation["user1"],
        conversation=parent_message.conversation,
        content="My own thread reply",
        parent_message=parent_message,
        reply_type="thread",
    )

    response = logged_in_client.get(
        f"/chat/message/{thread_reply.id}/load_for_thread_edit"
    )
    assert response.status_code == 200
    assert b"Editing Message" in response.data
    assert f'id="thread-input-container-{parent_message.id}"'.encode() in response.data


# --- Access-control regressions: message pagination + forwarding ---
#
# get_messages_page and forward_message only enforced @login_required, not
# conversation membership. That let any authenticated user page the history of
# an arbitrary channel/DM (guessable conversation_id + any message id as the
# cursor), and forward a message they couldn't see — now also exfiltrating its
# attachments. Both must return 403 for non-members.


def test_get_messages_page_denies_non_channel_member(logged_in_client, test_db):
    """Paging a channel the logged-in user isn't a member of is forbidden."""
    outsider = User.create(id=2, username="outsider", email="out@example.com")
    channel = Channel.create(workspace_id=1, name="secret-channel")
    conv, _ = Conversation.get_or_create(
        conversation_id_str=f"channel_{channel.id}", type="channel"
    )
    ChannelMember.create(user=outsider, channel=channel)  # user1 (id=1) is NOT a member
    secret_msg = Message.create(user=outsider, conversation=conv, content="classified")

    older = logged_in_client.get(
        f"/chat/messages/channel_{channel.id}?before_message_id={secret_msg.id}"
    )
    newer = logged_in_client.get(
        f"/chat/messages/channel_{channel.id}?after_message_id={secret_msg.id}"
    )
    assert older.status_code == 403
    assert newer.status_code == 403


def test_get_messages_page_denies_dm_outsider(logged_in_client, test_db):
    """Paging a DM the logged-in user isn't part of is forbidden."""
    alice = User.create(id=2, username="alice", email="a@example.com")
    bob = User.create(id=3, username="bob", email="b@example.com")
    conv, _ = Conversation.get_or_create(
        conversation_id_str=f"dm_{alice.id}_{bob.id}", type="dm"
    )
    private_msg = Message.create(user=alice, conversation=conv, content="private dm")

    res = logged_in_client.get(
        f"/chat/messages/dm_{alice.id}_{bob.id}?before_message_id={private_msg.id}"
    )
    assert res.status_code == 403


def test_forward_denies_inaccessible_source(logged_in_client, test_db):
    """Forwarding a message from a conversation the user can't see is forbidden."""
    user1 = User.get_by_id(1)
    outsider = User.create(id=2, username="outsider", email="out@example.com")

    # Source channel the user is NOT a member of.
    secret = Channel.create(workspace_id=1, name="secret")
    secret_conv, _ = Conversation.get_or_create(
        conversation_id_str=f"channel_{secret.id}", type="channel"
    )
    ChannelMember.create(user=outsider, channel=secret)
    secret_msg = Message.create(
        user=outsider, conversation=secret_conv, content="classified"
    )

    # Target channel the user IS a member of.
    mine = Channel.create(workspace_id=1, name="mine")
    Conversation.get_or_create(conversation_id_str=f"channel_{mine.id}", type="channel")
    ChannelMember.create(user=user1, channel=mine)

    res = logged_in_client.post(
        f"/chat/message/{secret_msg.id}/forward",
        data={"conversation_id_str": f"channel_{mine.id}", "optional_note": "leak"},
    )
    assert res.status_code == 403
