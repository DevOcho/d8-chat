# tests/test_chat_manager.py

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


def test_send_to_user(chat_manager):
    """Tests sending targeted user messages with exclusions."""
    chat_manager.send_to_user(1, "Hello", exclude_channel="chan_1")

    expected_payload = json.dumps({"_raw_html": "Hello", "_exclude_channel": "chan_1"})
    chat_manager.redis_client.publish.assert_called_once_with(
        "user:1", expected_payload
    )


def test_handle_typing_event(chat_manager):
    """Tests typing event additions, removals, and broadcasts."""
    mock_user = Mock()
    mock_user.username = "testuser"  # Explicitly set as a string
    mock_user.id = 1  # Add a serializable ID
    mock_ws = Mock()
    mock_ws.user = mock_user  # Attach the mock user to the mock websocket

    # Start typing
    chat_manager.handle_typing_event("chan_1", mock_user, True, mock_ws)
    assert "testuser" in chat_manager.typing_users["chan_1"]
    chat_manager.redis_client.publish.assert_called_once()

    # Stop typing
    chat_manager.handle_typing_event("chan_1", mock_user, False, mock_ws)
    assert "testuser" not in chat_manager.typing_users["chan_1"]


def test_is_user_online_in_cluster(chat_manager):
    """Tests checking online status checks Redis set."""
    chat_manager.redis_client.sismember.return_value = True
    assert chat_manager.is_user_online_in_cluster(1) is True


def test_handle_disconnect(chat_manager):
    """Tests the cleanup when a client disconnects."""
    mock_ws = Mock()
    chat_manager.all_clients[1] = mock_ws
    chat_manager.clients.add(mock_ws)

    chat_manager._handle_disconnect(mock_ws)

    assert 1 not in chat_manager.all_clients
    assert mock_ws not in chat_manager.clients


def test_send_message_success(chat_manager):
    """Tests _send_message safely strips internal keys and dispatches."""
    mock_ws = Mock()
    mock_ws.is_api_client = False
    # Add api_data to ensure it gets stripped for web clients
    payload = {"type": "test", "_sender_id": 1, "api_data": {"foo": "bar"}}

    chat_manager._send_message(mock_ws, payload)

    # ensure _sender_id and api_data were stripped for non-API clients
    mock_ws.send.assert_called_once_with('{"type": "test"}')


def test_send_message_api_client(chat_manager):
    """Tests that API clients get the api_data dictionary."""
    mock_ws = Mock()
    mock_ws.is_api_client = True
    payload = {
        "_raw_html": "<p>Hello</p>",
        "api_data": {"type": "new_message", "data": "Hello"},
    }

    chat_manager._send_message(mock_ws, payload)

    # ensure the API client only receives the JSON representation
    mock_ws.send.assert_called_once_with('{"type": "new_message", "data": "Hello"}')


def test_send_message_api_client_generic_event(chat_manager):
    """Tests that API clients receive generic events without _raw_html."""
    mock_ws = Mock()
    mock_ws.is_api_client = True
    payload = {"type": "typing_start", "_sender_id": 1}

    chat_manager._send_message(mock_ws, payload)

    # ensure internal tracking keys are stripped but the generic payload remains
    mock_ws.send.assert_called_once_with('{"type": "typing_start"}')


def test_send_message_exception(chat_manager):
    """Tests _send_message failing correctly calls _handle_disconnect."""
    mock_ws = Mock()
    mock_ws.send.side_effect = Exception("Socket Closed")

    chat_manager.all_clients[1] = mock_ws
    chat_manager.clients.add(mock_ws)

    # This should trigger an exception catch and disconnect
    chat_manager._send_message(mock_ws, "test html")

    assert 1 not in chat_manager.all_clients
