# tests/test_chat_manager.py

import json
from unittest.mock import Mock

import pytest

from app.chat_manager import ChatManager


class _LoopExit(BaseException):
    """Sentinel to break the supervised listener loop in tests.

    A BaseException (not Exception) so it escapes the loop's ``except
    Exception`` reconnect guard and unwinds ``listen_for_messages`` cleanly.
    """


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
    # unsubscribe now correctly fires a typing-stop broadcast, which serializes
    # user.username via JSON. Give the mock a real string username so the
    # broadcast doesn't fail with TypeError on the Mock attribute.
    mock_ws.user.username = "testuser"
    mock_ws.user.id = 1
    conv_id = "channel_123"

    chat_manager.subscribe(conv_id, mock_ws)
    assert mock_ws.channel_id == conv_id

    chat_manager.unsubscribe(mock_ws)
    assert mock_ws.channel_id is None


def test_unsubscribe_broadcasts_typing_stop(chat_manager):
    """Regression: unsubscribe must broadcast a typing-stop while channel_id is
    still set, otherwise typing indicators get stuck on after a disconnect."""
    mock_ws = Mock()
    mock_ws.channel_id = "channel_42"
    mock_ws.user.username = "alice"
    mock_ws.user.id = 7

    # Pre-populate the typing set so we can assert it's cleared.
    chat_manager.typing_users["channel_42"] = {"alice"}

    chat_manager.unsubscribe(mock_ws)

    assert "alice" not in chat_manager.typing_users.get("channel_42", set())
    chat_manager.redis_client.publish.assert_called()  # typing-stop was broadcast


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
    mock_ws.channel_id = None  # otherwise auto-Mock makes it truthy
    mock_ws.user.username = "testuser"
    mock_ws.user.id = 1
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
    mock_ws.channel_id = None  # otherwise auto-Mock makes it truthy
    mock_ws.user.username = "testuser"
    mock_ws.user.id = 1
    mock_ws.send.side_effect = Exception("Socket Closed")

    chat_manager.all_clients[1] = mock_ws
    chat_manager.clients.add(mock_ws)

    # This should trigger an exception catch and disconnect
    chat_manager._send_message(mock_ws, "test html")

    assert 1 not in chat_manager.all_clients


# --- Supervised listener loop (Phase 1.1) ---


def test_listener_dispatch_error_does_not_kill_loop(app, chat_manager, mocker):
    """A dispatch failure on one message must not stop the loop; the next
    message is still processed."""
    with app.app_context():

        def _get(**_kwargs):
            try:
                return next(seq)
            except StopIteration:
                raise _LoopExit()

        seq = iter(
            [
                {"type": "pmessage", "channel": b"chat:c", "data": "{}"},
                {"type": "pmessage", "channel": b"chat:c", "data": "{}"},
            ]
        )
        fake_pubsub = Mock()
        fake_pubsub.get_message.side_effect = _get
        chat_manager.redis_client.pubsub.return_value = fake_pubsub
        # redis_client is already set by the fixture; keep initialize a no-op.
        mocker.patch.object(chat_manager, "initialize")

        dispatch = mocker.patch.object(
            chat_manager, "_dispatch", side_effect=[RuntimeError("bad"), None]
        )

        with pytest.raises(_LoopExit):
            chat_manager.listen_for_messages()

        assert dispatch.call_count == 2  # error on the first didn't stop the second


def test_listener_reconnects_after_connection_error(app, chat_manager, mocker):
    """A ConnectionError from the inner loop triggers a supervised reconnect
    rather than killing the listener thread."""
    with app.app_context():
        pubsub_bad = Mock()
        pubsub_bad.get_message.side_effect = ConnectionError("valkey down")

        def _get(**_kwargs):
            try:
                return next(seq)
            except StopIteration:
                raise _LoopExit()

        seq = iter([{"type": "pmessage", "channel": b"chat:c", "data": "{}"}])
        pubsub_good = Mock()
        pubsub_good.get_message.side_effect = _get

        # The same client object is restored after _reset_redis nulls it, and
        # hands out the bad pubsub first, then the good one on reconnect.
        client = chat_manager.redis_client
        client.pubsub.side_effect = [pubsub_bad, pubsub_good]

        def _fake_init(_app):
            if chat_manager.redis_client is None:
                chat_manager.redis_client = client

        mocker.patch.object(chat_manager, "initialize", side_effect=_fake_init)
        mocker.patch("app.chat_manager.time.sleep")  # no real backoff delay
        dispatch = mocker.patch.object(chat_manager, "_dispatch")

        with pytest.raises(_LoopExit):
            chat_manager.listen_for_messages()

        assert chat_manager.listener_restarts >= 1
        assert dispatch.call_count == 1  # the post-reconnect message got through


def test_dispatch_routes_channel_message_to_subscribed_client(app, chat_manager):
    """_dispatch delivers a chat:* message only to clients on that channel."""
    with app.app_context():
        on_channel = Mock()
        on_channel.channel_id = "channel_1"
        on_channel.is_api_client = False
        off_channel = Mock()
        off_channel.channel_id = "channel_2"
        off_channel.is_api_client = False
        chat_manager.clients = {on_channel, off_channel}

        message = {
            "type": "pmessage",
            "channel": b"chat:channel_1",
            "data": json.dumps({"_raw_html": "<p>hi</p>"}),
        }
        chat_manager._dispatch(message)

        on_channel.send.assert_called_once()
        off_channel.send.assert_not_called()
