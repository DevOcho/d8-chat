# app/chat_manager.py
"""Module for managing WebSocket connections and Redis Pub/Sub."""

# pylint: disable=import-error

import datetime
import json

import redis
from flask import current_app

from .models import Conversation, UserConversationStatus


class ChatManager:
    """Manages WebSocket clients, online status, and Redis Pub/Sub broadcasting."""

    def __init__(self):
        self.clients = set()
        self.online_users = {}
        self.all_clients = {}
        self.typing_users = {}
        self.redis_client = None
        self.pubsub = None

    def initialize(self, app):
        """Initializes the Valkey/Redis connection."""
        if self.redis_client is None:
            self.redis_client = redis.from_url(app.config["VALKEY_URL"])
            self.pubsub = self.redis_client.pubsub()

    def listen_for_messages(self):
        """A background task to listen for messages from Valkey and forward them to clients."""
        if not self.pubsub:
            self.initialize(current_app)

        self.pubsub.psubscribe("chat:*", "user:*", "global:*")
        print("Subscribed to Valkey/Redis Pub/Sub channels.")

        for message in self.pubsub.listen():
            if message["type"] != "pmessage":
                continue

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
                        continue

                    self._send_message(ws, payload_data)
                continue

            target_channel = None
            if channel_name.startswith("chat:"):
                target_channel = channel_name.split(":", 1)[1]

            for client_ws in clients_on_this_pod:
                # For a channel message, only send to clients subscribed to that channel.
                if target_channel:
                    if (
                        hasattr(client_ws, "channel_id")
                        and client_ws.channel_id == target_channel
                    ):
                        self._send_message(client_ws, payload_data)
                # For a global message, send to everyone.
                elif channel_name.startswith("global:"):
                    self._send_message(client_ws, payload_data)

    def _send_message(self, ws, message):
        try:
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
                        ws.send(json.dumps(clean_payload))
                    return

                # Web clients prefer the HTML payload if provided
                payload_to_send = message.get("_raw_html") or message
                if isinstance(payload_to_send, dict):
                    clean_payload = payload_to_send.copy()
                    clean_payload.pop("_sender_id", None)
                    clean_payload.pop("_exclude_channel", None)
                    clean_payload.pop("api_data", None)
                    ws.send(json.dumps(clean_payload))
                else:
                    ws.send(str(payload_to_send))
            else:
                # Fallback for plain string messages (often sent in tests)
                ws.send(str(message))
        except Exception as e:  # pylint: disable=broad-exception-caught
            print(f"Error sending to client {ws}: {e}")
            self._handle_disconnect(ws)

    def broadcast(self, channel_id, message, sender_ws=None):
        """Publishes a message to a specific channel on Valkey."""
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
        print(f"Client {ws} subscribed to channel {ws.channel_id}")

    def unsubscribe(self, ws):
        """Unsubscribes a websocket from its current channel, updating read status."""
        if hasattr(ws, "channel_id") and ws.channel_id:
            channel_id = ws.channel_id
            if hasattr(ws, "user") and ws.user:
                try:
                    conv = Conversation.get_or_none(conversation_id_str=channel_id)
                    if conv:
                        UserConversationStatus.update(
                            last_read_timestamp=datetime.datetime.now()
                        ).where(
                            (UserConversationStatus.user == ws.user)
                            & (UserConversationStatus.conversation == conv)
                        ).execute()
                except Exception as e:  # pylint: disable=broad-exception-caught
                    print(f"Error updating last_read_timestamp: {e}")
            print(f"Client {ws} unsubscribed from channel {channel_id}")
            ws.channel_id = None
        user = getattr(ws, "user", None)
        if user and hasattr(ws, "channel_id") and ws.channel_id:
            self.handle_typing_event(ws.channel_id, user, is_typing=False, sender_ws=ws)

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
        self.broadcast(conversation_id, payload, sender_ws=sender_ws)


chat_manager = ChatManager()
