import json
from unittest.mock import Mock

import pytest

from app.chat_manager import ChatManager


@pytest.fixture
def chat_manager(mocker):
    """
    Returns a fresh instance of ChatManager for each test and mocks the
    Redis/Valkey client.
    """
    # We must patch the redis client *before* the ChatManager is instantiated
    mock_redis_client = Mock()
    mocker.patch("redis.from_url", return_value=mock_redis_client)

    manager = ChatManager()
    # The initialize call sets up self.redis_client, so we re-assign our mock
    manager.redis_client = mock_redis_client
    return manager


def test_user_presence(chat_manager):
    """Tests setting users online and offline."""
    mock_ws = Mock()
    user_id = 1
    assert not chat_manager.is_online(user_id)
    chat_manager.set_online(user_id, mock_ws)
    assert chat_manager.is_online(user_id)
    assert chat_manager.all_clients[user_id] is mock_ws
    chat_manager.set_offline(user_id)
    assert not chat_manager.is_online(user_id)


def test_subscribe_and_unsubscribe(chat_manager):
    """Tests that subscribing and unsubscribing sets the channel_id on the websocket object."""
    mock_ws = Mock()
    mock_ws.channel_id = None
    conv_id = "channel_123"

    chat_manager.subscribe(conv_id, mock_ws)
    assert mock_ws.channel_id == conv_id

    chat_manager.unsubscribe(mock_ws)
    assert mock_ws.channel_id is None


def test_broadcast_to_channel(chat_manager):
    """Tests that broadcasting to a channel publishes to the correct Redis channel."""
    conv_id = "channel_abc"
    message = "<p>Hello</p>"
    sender_ws = Mock()
    sender_ws.user.id = 1  # Mock the sender's user ID

    chat_manager.broadcast(conv_id, message, sender_ws=sender_ws)

    # Assert that publish was called on our mocked redis client
    expected_channel = f"chat:{conv_id}"
    expected_payload = json.dumps({"_raw_html": message, "_sender_id": 1})
    chat_manager.redis_client.publish.assert_called_once_with(
        expected_channel, expected_payload
    )


def test_broadcast_to_all(chat_manager):
    """Tests that broadcasting to all publishes to the global Redis channel."""
    message = "<p>Global Update</p>"

    chat_manager.broadcast_to_all(message)

    expected_payload = json.dumps({"_raw_html": message})
    chat_manager.redis_client.publish.assert_called_once_with(
        "global:events", expected_payload
    )
