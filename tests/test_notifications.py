# tests/test_notifications.py
import datetime
from unittest.mock import Mock, call

import pytest
from flask import render_template, url_for

from app.models import (
    Channel,
    ChannelMember,
    Conversation,
    User,
    UserConversationStatus,
)
from app.services import chat_service


@pytest.fixture
def setup_mention_test(test_db):
    """
    Sets up a sender and a recipient in a channel for testing notifications.
    """
    sender = User.create(id=2, username="sender", email="sender@test.com")
    recipient = User.get_by_id(1)  # Our default logged-in user

    channel = Channel.get(name="general")
    ChannelMember.create(user=sender, channel=channel)
    # Ensure recipient is also a member
    ChannelMember.get_or_create(user=recipient, channel=channel)

    conversation = Conversation.get(conversation_id_str=f"channel_{channel.id}")

    # Set the recipient's last read time to the past so the new message is "unread"
    status, _ = UserConversationStatus.get_or_create(
        user=recipient, conversation=conversation
    )
    status.last_read_timestamp = datetime.datetime.now() - datetime.timedelta(hours=1)
    status.save()

    return {
        "sender": sender,
        "recipient": recipient,
        "conversation": conversation,
        "channel": channel,
    }


def test_channel_mention_sends_badge_notification(app, setup_mention_test, mocker):
    """
    GIVEN a sender and an online recipient in a channel
    WHEN the sender posts a message mentioning the recipient
    THEN the notification service should send the correct badge HTML to the recipient.
    """
    sender = setup_mention_test["sender"]
    recipient = setup_mention_test["recipient"]
    conversation = setup_mention_test["conversation"]
    channel = setup_mention_test["channel"]

    # Mock the chat_manager to spy on its methods
    mock_chat_manager = mocker.patch("app.services.chat_service.chat_manager")
    # Simulate that the recipient is online
    mock_chat_manager.all_clients = {recipient.id: Mock()}

    # Create the new message with a mention
    new_message = chat_service.handle_new_message(
        sender=sender,
        conversation=conversation,
        chat_text=f"Hello @{recipient.username}, this is a test.",
    )

    # --- ACT ---
    # Manually call the notification function we are testing
    # We must do this within an app context to use url_for
    with app.app_context():
        chat_service.send_notifications_for_new_message(new_message, sender)

    # --- ASSERT ---
    # 1. Generate the exact HTML we expect to be sent
    with app.app_context():
        expected_badge_html = render_template(
            "partials/unread_badge.html",
            conv_id_str=conversation.conversation_id_str,
            count=1,  # We expect a count of 1 for this new mention
            link_text=f"# {channel.name}",
            hx_get_url=url_for("channels.get_channel_chat", channel_id=channel.id),
        )
        expected_unreads_link_html = render_template(
            "partials/unreads_link_unread.html"
        )

    # 2. Check that send_to_user was called with the correct arguments
    # We use call objects to check for multiple calls
    expected_calls = [
        call(recipient.id, expected_badge_html),
        call(recipient.id, expected_unreads_link_html),
        call(recipient.id, {"type": "sound"}),
        # We can also check parts of the desktop notification payload
    ]

    mock_chat_manager.send_to_user.assert_has_calls(expected_calls, any_order=True)

    # Verify the desktop notification payload is correct
    found_notification_call = False
    for sent_call in mock_chat_manager.send_to_user.call_args_list:
        args, kwargs = sent_call
        if isinstance(args[1], dict) and args[1].get("type") == "notification":
            found_notification_call = True
            assert args[1]["title"] == f"New mention from {sender.username}"
            assert args[1]["body"] == new_message.content
            break
    assert found_notification_call, "Desktop notification payload was not sent"
