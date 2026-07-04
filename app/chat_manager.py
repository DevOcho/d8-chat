# app/chat_manager.py
"""Module for managing WebSocket connections and Redis Pub/Sub."""

# pylint: disable=import-error

import json
import time

import redis
from flask import current_app

from .models import Conversation, UserConversationStatus, utc_now
from .ws_utils import LOCK_TYPES


class ChatManager:
    """Manages WebSocket clients, online status, and Redis Pub/Sub broadcasting."""

    def __init__(self):
        self.clients = set()
        self.online_users = {}
        self.all_clients = {}
        self.typing_users = {}
        self.redis_client = None
        self.pubsub = None
        # Liveness + observability for the background listener thread. The
        # /healthz endpoint reads listener_heartbeat to detect a wedged or dead
        # listener; listener_restarts counts supervised reconnects.
        self.listener_heartbeat = 0.0
        self.listener_restarts = 0
        self._last_presence_heartbeat = 0.0

    def initialize(self, app):
        """Initializes the Valkey/Redis connection."""
        if self.redis_client is None:
            # health_check_interval + socket_keepalive keep the long-lived
            # pub/sub connection honest: redis-py periodically pings, and TCP
            # keepalive tears down a silently-dropped connection instead of
            # leaving the listener blocked forever on a dead socket.
            self.redis_client = redis.from_url(
                app.config["VALKEY_URL"],
                health_check_interval=30,
                socket_keepalive=True,
                socket_connect_timeout=5,
                retry_on_timeout=True,
            )
            self.pubsub = self.redis_client.pubsub()

    def _reset_redis(self):
        """Drop the cached client/pubsub so the next initialize() rebuilds them.

        Called after the listener loop dies so a fresh connection is
        established on the next supervised retry.
        """
        try:
            if self.pubsub is not None:
                self.pubsub.close()
        except Exception:  # pylint: disable=broad-exception-caught
            pass
        try:
            if self.redis_client is not None:
                self.redis_client.close()
        except Exception:  # pylint: disable=broad-exception-caught
            pass
        self.pubsub = None
        self.redis_client = None

    def listen_for_messages(self):
        """Background task: forward Valkey pub/sub messages to local clients.

        Supervised: if the Redis connection drops or any error escapes the
        inner loop, we log it, rebuild the client, and reconnect with capped
        exponential backoff. Previously a single ConnectionError (or one bad
        payload) killed this daemon thread silently — sockets stayed open but
        nothing was ever delivered again until the pod restarted.
        """
        backoff = 1
        while True:
            try:
                self.initialize(current_app)
                pubsub = self.redis_client.pubsub()
                pubsub.psubscribe("chat:*", "user:*", "global:*")
                current_app.logger.info(
                    "WS listener subscribed to Valkey/Redis pub/sub."
                )
                backoff = 1  # reset after a clean (re)subscribe

                while True:
                    # timeout wakes us periodically even when idle so we can
                    # stamp the heartbeat and run the presence sweep.
                    message = pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=5.0
                    )
                    self.listener_heartbeat = time.time()
                    self._heartbeat_presence_maybe()

                    if message is None or message.get("type") != "pmessage":
                        continue

                    try:
                        self._dispatch(message)
                    except Exception:  # pylint: disable=broad-exception-caught
                        current_app.logger.exception(
                            "WS dispatch failed; message skipped"
                        )
                    finally:
                        # Never let the listener pin a pooled DB connection —
                        # _dispatch may open one (unsubscribe writes read state).
                        self._close_db_if_open()
            except Exception:  # pylint: disable=broad-exception-caught
                self.listener_restarts += 1
                current_app.logger.exception(
                    "WS listener died; reconnecting in %ss (restart #%s)",
                    backoff,
                    self.listener_restarts,
                )
                self._reset_redis()
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)

    @staticmethod
    def _close_db_if_open():
        """Return any DB connection this thread checked out to the pool."""
        # pylint: disable=import-outside-toplevel
        from .models import db

        try:
            if not current_app.testing and not db.is_closed():
                db.close()
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    def _heartbeat_presence_maybe(self):
        """Hook for periodic cluster-presence heartbeat (Phase 3.2).

        No-op placeholder until TTL-based presence lands; keeping the call site
        here means the listener's 5s wake-up already drives it.
        """

    def _dispatch(self, message):
        """Fan a single pub/sub pmessage out to the matching local clients."""
        channel_name = message["channel"].decode("utf-8")
        payload_data = json.loads(message["data"])

        # Create a safe copy of clients to iterate over for this pod
        clients_on_this_pod = list(self.clients)

        if channel_name.startswith("user:"):
            target_user_id = int(channel_name.split(":", 1)[1])
            if target_user_id in self.all_clients:
                ws = self.all_clients[target_user_id]

                # Filter out messages meant to be excluded for the active channel
                exclude_channel = payload_data.get("_exclude_channel")
                if (
                    exclude_channel
                    and getattr(ws, "channel_id", None) == exclude_channel
                ):
                    return

                self._send_message(ws, payload_data)
            return

        target_channel = None
        if channel_name.startswith("chat:"):
            target_channel = channel_name.split(":", 1)[1]

        # Only set when a broadcast explicitly opts out of echoing to its
        # sender (typing events). Messages deliberately echo back to the
        # sender so their own message renders, so they leave this unset.
        exclude_sender_id = (
            payload_data.get("_sender_id")
            if payload_data.get("_exclude_sender")
            else None
        )

        for client_ws in clients_on_this_pod:
            # For a channel message, only send to clients subscribed to that channel.
            if target_channel:
                if (
                    hasattr(client_ws, "channel_id")
                    and client_ws.channel_id == target_channel
                ):
                    # Don't echo a sender's own typing event back to them.
                    # typing_users is per-worker, so the sender's worker
                    # broadcasts only [self] while the other person's worker
                    # broadcasts [them]; the sender's client receiving its
                    # own [self] (filtered to empty) interleaved with [them]
                    # makes the "X is typing" indicator strobe.
                    if (
                        exclude_sender_id is not None
                        and getattr(getattr(client_ws, "user", None), "id", None)
                        == exclude_sender_id
                    ):
                        continue
                    self._send_message(client_ws, payload_data)
            # For a global message, send to everyone.
            elif channel_name.startswith("global:"):
                self._send_message(client_ws, payload_data)

    def _send_message(self, ws, message):
        try:
            # Serialize the whole encode+send under the connection's shared
            # reentrant lock so a broadcast and a targeted notification (or the
            # background ping frame) can't interleave on the wire. Test Mocks
            # have no real lock, so they take the direct path unchanged.
            lock = getattr(ws, "_d8_send_lock", None)
            if isinstance(lock, LOCK_TYPES):
                with lock:
                    self._encode_and_send(ws, message)
            else:
                self._encode_and_send(ws, message)
        except Exception:  # pylint: disable=broad-exception-caught
            current_app.logger.exception(f"Error sending to client {ws}")
            self._handle_disconnect(ws)

    def _encode_and_send(self, ws, message):
        is_api = getattr(ws, "is_api_client", False)

        # The payload from the listener or tests is usually a dictionary
        if isinstance(message, dict):
            if is_api:
                # API Clients exclusively get the structured JSON data if present
                if "api_data" in message:
                    ws.send(json.dumps(message["api_data"]))
                elif "_raw_html" not in message:
                    # Forward generic events (like typing or presence)
                    clean_payload = message.copy()
                    clean_payload.pop("_sender_id", None)
                    clean_payload.pop("_exclude_channel", None)
                    clean_payload.pop("_exclude_sender", None)
                    ws.send(json.dumps(clean_payload))
                return

            # Web clients prefer the HTML payload if provided
            payload_to_send = message.get("_raw_html") or message
            if isinstance(payload_to_send, dict):
                clean_payload = payload_to_send.copy()
                clean_payload.pop("_sender_id", None)
                clean_payload.pop("_exclude_channel", None)
                clean_payload.pop("_exclude_sender", None)
                clean_payload.pop("api_data", None)
                ws.send(json.dumps(clean_payload))
            else:
                ws.send(str(payload_to_send))
        else:
            # Fallback for plain string messages (often sent in tests)
            ws.send(str(message))

    def broadcast(self, channel_id, message, sender_ws=None, exclude_sender=False):
        """Publishes a message to a specific channel on Valkey.

        When exclude_sender is True, the listener skips delivering this payload
        back to the sender's own client (identified via _sender_id). Used for
        typing events; messages leave it False so the sender's client still
        receives and renders its own message.
        """
        redis_channel = f"chat:{channel_id}"
        sender_id = (
            sender_ws.user.id if sender_ws and hasattr(sender_ws, "user") else None
        )

        payload_data = {}
        if isinstance(message, dict):
            payload_data = message.copy()
        else:
            payload_data["_raw_html"] = message

        payload_data["_sender_id"] = sender_id
        if exclude_sender:
            payload_data["_exclude_sender"] = True
        self.redis_client.publish(redis_channel, json.dumps(payload_data))

    def send_to_user(self, user_id, message, exclude_channel=None):
        """Publishes a message to a user-specific channel on Valkey."""
        redis_channel = f"user:{user_id}"
        if isinstance(message, dict):
            payload_data = message.copy()
        else:
            payload_data = {"_raw_html": message}

        if exclude_channel:
            payload_data["_exclude_channel"] = exclude_channel

        self.redis_client.publish(redis_channel, json.dumps(payload_data))

    def _handle_disconnect(self, ws):
        user_id_to_remove = None
        for uid, client_ws in self.all_clients.items():
            if client_ws == ws:
                user_id_to_remove = uid
                break
        if user_id_to_remove:
            self.set_offline(user_id_to_remove)
        self.unsubscribe(ws)
        self.clients.discard(ws)

    def set_online(self, user_id, ws):
        """Marks a user as online and tracks their websocket connection."""
        self.clients.add(ws)
        self.online_users[user_id] = "online"
        self.all_clients[user_id] = ws
        if self.redis_client:
            self.redis_client.sadd("global:online_users", user_id)

    def set_offline(self, user_id):
        """Marks a user as offline and cleans up their state."""
        self.online_users.pop(user_id, None)
        self.all_clients.pop(user_id, None)
        if self.redis_client:
            self.redis_client.srem("global:online_users", user_id)

    def is_online(self, user_id):
        """Checks if a user is online in the current pod."""
        return user_id in self.online_users

    def is_user_online_in_cluster(self, user_id):
        """Checks if a user is online across ANY worker via Redis."""
        if self.redis_client:
            return self.redis_client.sismember("global:online_users", str(user_id))
        return user_id in self.online_users

    def broadcast_to_all(self, message):
        """Publishes a message to all users globally."""
        self.redis_client.publish("global:events", json.dumps({"_raw_html": message}))

    def subscribe(self, channel_id, ws):
        """Subscribes a websocket to a specific conversation channel."""
        self.unsubscribe(ws)
        ws.channel_id = str(channel_id)
        current_app.logger.debug(f"Client {ws} subscribed to channel {ws.channel_id}")

    def unsubscribe(self, ws):
        """Unsubscribes a websocket from its current channel, updating read status."""
        channel_id = getattr(ws, "channel_id", None)
        if not channel_id:
            return

        user = getattr(ws, "user", None)
        if user:
            try:
                conv = Conversation.get_or_none(conversation_id_str=channel_id)
                if conv:
                    UserConversationStatus.update(last_read_timestamp=utc_now()).where(
                        (UserConversationStatus.user == user)
                        & (UserConversationStatus.conversation == conv)
                    ).execute()
            except Exception:  # pylint: disable=broad-exception-caught
                current_app.logger.exception("Error updating last_read_timestamp")

            # Broadcast typing-stop *before* clearing channel_id. Previously
            # this lived after `ws.channel_id = None` and the guard
            # `if ws.channel_id` was always false — typing indicators got
            # stuck on after a disconnect.
            self.handle_typing_event(channel_id, user, is_typing=False, sender_ws=ws)

        current_app.logger.debug(f"Client {ws} unsubscribed from channel {channel_id}")
        ws.channel_id = None

    def handle_typing_event(self, conversation_id, user, is_typing, sender_ws):
        """Handles and broadcasts typing status updates."""
        if not conversation_id or not user:
            return
        self.typing_users.setdefault(conversation_id, set())
        if is_typing:
            self.typing_users[conversation_id].add(user.username)
        else:
            self.typing_users[conversation_id].discard(user.username)
        typists = list(self.typing_users.get(conversation_id, list()))
        payload = {
            "type": "typing_update",
            "conversation_id": conversation_id,
            "typists": typists,
        }
        self.broadcast(
            conversation_id, payload, sender_ws=sender_ws, exclude_sender=True
        )


chat_manager = ChatManager()
