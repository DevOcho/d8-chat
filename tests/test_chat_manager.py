# tests/test_chat_manager.py

import pytest
from unittest.mock import Mock
from app.chat_manager import ChatManager


@pytest.fixture
def chat_manager():
    """Returns a fresh instance of ChatManager for each test."""
    return ChatManager()


def test_user_presence(chat_manager):
    """
    Tests setting users online and offline.
    """
    mock_ws = Mock()
    user_id = 1

    assert not chat_manager.is_online(user_id)

    # Test setting user online
    chat_manager.set_online(user_id, mock_ws)
    assert chat_manager.is_online(user_id)
    assert chat_manager.online_users[user_id] == "online"
    assert chat_manager.all_clients[user_id] is mock_ws

    # Test setting user offline
    chat_manager.set_offline(user_id)
    assert not chat_manager.is_online(user_id)
    assert user_id not in chat_manager.online_users
    assert user_id not in chat_manager.all_clients


def test_subscribe_and_unsubscribe(chat_manager):
    """
    Tests subscribing and unsubscribing a client to a channel.
    """
    mock_ws = Mock()
    # Mock the channel_id attribute that gets set on the real ws object
    mock_ws.channel_id = None
    conv_id = "channel_123"

    # Subscribe
    chat_manager.subscribe(conv_id, mock_ws)
    assert mock_ws in chat_manager.active_connections[conv_id]
    assert mock_ws.channel_id == conv_id

    # Unsubscribe
    chat_manager.unsubscribe(mock_ws)
    assert conv_id not in chat_manager.active_connections
    assert mock_ws.channel_id is None


def test_broadcast_to_channel(chat_manager):
    """
    Tests broadcasting a message to a specific channel.
    """
    ws1 = Mock()
    ws2 = Mock()
    ws3 = Mock()
    conv_id = "channel_abc"
    message = "<p>Hello</p>"

    chat_manager.subscribe(conv_id, ws1)
    chat_manager.subscribe(conv_id, ws2)
    # ws3 is not subscribed to this channel

    # Broadcast to the channel, excluding the sender (ws1)
    chat_manager.broadcast(conv_id, message, sender_ws=ws1)

    # ws1 (sender) should not have been sent the message
    ws1.send.assert_not_called()
    # ws2 (subscriber) should have been sent the message
    ws2.send.assert_called_once_with(message)
    # ws3 (not in channel) should not have been sent the message
    ws3.send.assert_not_called()


def test_broadcast_to_all(chat_manager):
    """
    Tests broadcasting a message to all connected clients.
    """
    ws1 = Mock()
    ws2 = Mock()
    user1_id = 1
    user2_id = 2
    message = "<p>Global Update</p>"

    chat_manager.set_online(user1_id, ws1)
    chat_manager.set_online(user2_id, ws2)

    chat_manager.broadcast_to_all(message)

    ws1.send.assert_called_once_with(message)
    ws2.send.assert_called_once_with(message)


def test_broadcast_handles_exceptions(chat_manager):
    """
    Tests that the broadcast function handles exceptions and removes bad clients.
    """
    ws1 = Mock()
    ws2 = Mock()
    # Configure the send method on ws2 to raise an exception when called
    ws2.send.side_effect = Exception("Connection closed")
    ws3 = Mock()

    conv_id = "channel_error"
    message = "<p>test</p>"

    chat_manager.subscribe(conv_id, ws1)
    chat_manager.subscribe(conv_id, ws2)  # The "bad" client
    chat_manager.subscribe(conv_id, ws3)

    assert len(chat_manager.active_connections[conv_id]) == 3

    # Act: Broadcast a message
    chat_manager.broadcast(conv_id, message)

    # Assert
    # ws1 should have received the message successfully
    ws1.send.assert_called_once_with(message)
    # ws2's send method was called, but it raised an error
    ws2.send.assert_called_once_with(message)
    # ws3 should have also received the message successfully
    ws3.send.assert_called_once_with(message)

    # The most important check: the bad client (ws2) should have been removed
    assert ws2 not in chat_manager.active_connections[conv_id]
    assert len(chat_manager.active_connections[conv_id]) == 2


def test_broadcast_to_all_handles_exceptions(chat_manager, mocker):
    """
    Covers: Exception handling in `broadcast_to_all`.
    """
    ws1 = mocker.Mock()
    ws2 = mocker.Mock()
    ws2.send.side_effect = Exception("Broken pipe")  # This client will fail

    chat_manager.set_online(1, ws1)
    chat_manager.set_online(2, ws2)

    # This should execute without raising an exception
    try:
        chat_manager.broadcast_to_all("test message")
    except Exception:
        pytest.fail("broadcast_to_all should not propagate exceptions.")

    # Verify that send was still called on both
    ws1.send.assert_called_once_with("test message")
    ws2.send.assert_called_once_with("test message")
