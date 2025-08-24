import pytest
from app.services import chat_service
from app.models import (
    User,
    Channel,
    ChannelMember,
    WorkspaceMember,
    Conversation,
    Message,
    Mention,
)
from app.chat_manager import chat_manager


@pytest.fixture
def setup_channel_and_users(test_db):
    """
    Sets up a standard testing environment with a channel and three users.
    - user1: The default logged-in user who will be the sender.
    - user2: A member of the channel.
    - user3: A member of the channel.
    """
    user1 = User.get_by_id(1)
    user2 = User.create(id=2, username="zelda", email="zelda@example.com")
    user3 = User.create(id=3, username="link", email="link@example.com")

    workspace = WorkspaceMember.get(user=user1).workspace
    WorkspaceMember.create(user=user2, workspace=workspace)
    WorkspaceMember.create(user=user3, workspace=workspace)

    channel = Channel.create(workspace=workspace, name="test-service-channel")
    conv, _ = Conversation.get_or_create(
        conversation_id_str=f"channel_{channel.id}", type="channel"
    )

    ChannelMember.create(user=user1, channel=channel)
    ChannelMember.create(user=user2, channel=channel)
    ChannelMember.create(user=user3, channel=channel)

    return {"sender": user1, "user2": user2, "user3": user3, "conversation": conv}


def test_handle_new_message_creates_message(setup_channel_and_users):
    """
    Tests that a basic message is created successfully.
    """
    sender = setup_channel_and_users["sender"]
    conversation = setup_channel_and_users["conversation"]

    assert Message.select().count() == 0

    new_message = chat_service.handle_new_message(
        sender=sender,
        conversation=conversation,
        chat_text="Hello world!",
        parent_id=None,
    )

    assert Message.select().count() == 1
    assert new_message.content == "Hello world!"
    assert new_message.user == sender
    assert Mention.select().count() == 0


def test_handle_new_message_creates_username_mentions(setup_channel_and_users):
    """
    Tests that standard @username mentions are created correctly.
    """
    sender = setup_channel_and_users["sender"]
    user2 = setup_channel_and_users["user2"]
    conversation = setup_channel_and_users["conversation"]

    new_message = chat_service.handle_new_message(
        sender=sender,
        conversation=conversation,
        chat_text=f"Hey @{user2.username}, can you check this?",
    )

    assert Mention.select().count() == 1
    mention = Mention.get()
    assert mention.user == user2
    assert mention.message == new_message


def test_handle_new_message_creates_channel_mentions(setup_channel_and_users):
    """
    Tests that @channel creates mentions for all channel members except the sender.
    """
    sender = setup_channel_and_users["sender"]
    user2 = setup_channel_and_users["user2"]
    user3 = setup_channel_and_users["user3"]
    conversation = setup_channel_and_users["conversation"]

    chat_service.handle_new_message(
        sender=sender, conversation=conversation, chat_text="Attention @channel!"
    )

    # Should be 2 mentions: user2 and user3. The sender (user1) is excluded.
    assert Mention.select().count() == 2
    mentioned_user_ids = {m.user.id for m in Mention.select()}
    assert mentioned_user_ids == {user2.id, user3.id}


def test_handle_new_message_creates_here_mentions_for_online_users(
    setup_channel_and_users, mocker
):
    """
    Tests that @here only creates mentions for online members.
    """
    sender = setup_channel_and_users["sender"]
    user2 = setup_channel_and_users["user2"]
    user3 = setup_channel_and_users["user3"]  # This user will be "offline"
    conversation = setup_channel_and_users["conversation"]

    # Mock the chat_manager to simulate only sender and user2 being online
    mocker.patch.dict(
        chat_manager.online_users, {sender.id: "online", user2.id: "online"}, clear=True
    )

    chat_service.handle_new_message(
        sender=sender, conversation=conversation, chat_text="Are you @here?"
    )

    # Should be 1 mention for user2. Sender is excluded, user3 is offline.
    assert Mention.select().count() == 1
    mention = Mention.get()
    assert mention.user == user2


def test_handle_new_message_creates_no_self_mentions(setup_channel_and_users):
    """
    Tests that users mentioning themselves do not create a Mention record.
    """
    sender = setup_channel_and_users["sender"]
    conversation = setup_channel_and_users["conversation"]

    chat_service.handle_new_message(
        sender=sender,
        conversation=conversation,
        chat_text=f"A note to myself, @{sender.username}",
    )

    assert Mention.select().count() == 0
