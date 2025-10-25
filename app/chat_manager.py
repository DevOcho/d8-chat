import datetime
import json

import redis
from flask import current_app

from .models import Conversation, UserConversationStatus


class ChatManager:
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

            # Determine what to send: raw HTML or a JSON object
            payload_to_send = payload_data.get("_raw_html") or payload_data

            # Create a safe copy of clients to iterate over for this pod
            clients_on_this_pod = list(self.clients)

            if channel_name.startswith("user:"):
                target_user_id = int(channel_name.split(":", 1)[1])
                if target_user_id in self.all_clients:
                    self._send_message(
                        self.all_clients[target_user_id], payload_to_send
                    )
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
                        self._send_message(client_ws, payload_to_send)
                # For a global message, send to everyone.
                elif channel_name.startswith("global:"):
                    self._send_message(client_ws, payload_to_send)

    def _send_message(self, ws, message):
        try:
            # The payload from the listener might be a dict (typing) or str (HTML)
            if isinstance(message, dict):
                message.pop("_sender_id", None)
                payload = json.dumps(message)
            else:
                payload = str(message)
            ws.send(payload)
        except Exception as e:
            print(f"Error sending to client {ws}: {e}")
            self._handle_disconnect(ws)

    def broadcast(self, channel_id, message, sender_ws=None, is_event=False):
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

    def send_to_user(self, user_id, message):
        """Publishes a message to a user-specific channel on Valkey."""
        redis_channel = f"user:{user_id}"
        if isinstance(message, dict):
            payload = json.dumps(message)
        else:
            payload = json.dumps({"_raw_html": message})
        self.redis_client.publish(redis_channel, payload)

    # --- Other methods remain largely the same ---

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
        self.clients.add(ws)
        self.online_users[user_id] = "online"
        self.all_clients[user_id] = ws

    def set_offline(self, user_id):
        self.online_users.pop(user_id, None)
        self.all_clients.pop(user_id, None)

    def is_online(self, user_id):
        return user_id in self.online_users

    def broadcast_to_all(self, message):
        self.redis_client.publish("global:events", json.dumps({"_raw_html": message}))

    def subscribe(self, channel_id, ws):
        self.unsubscribe(ws)
        ws.channel_id = str(channel_id)
        print(f"Client {ws} subscribed to channel {ws.channel_id}")

    def unsubscribe(self, ws):
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
                except Exception as e:
                    print(f"Error updating last_read_timestamp: {e}")
            print(f"Client {ws} unsubscribed from channel {channel_id}")
            ws.channel_id = None
        user = getattr(ws, "user", None)
        if user and hasattr(ws, "channel_id") and ws.channel_id:
            self.handle_typing_event(ws.channel_id, user, is_typing=False, sender_ws=ws)

    def handle_typing_event(self, conversation_id, user, is_typing, sender_ws):
        if not conversation_id or not user:
            return
        self.typing_users.setdefault(conversation_id, set())
        if is_typing:
            self.typing_users[conversation_id].add(user.username)
        else:
            self.typing_users[conversation_id].discard(user.username)
        typists = list(self.typing_users.get(conversation_id, []))
        payload = {"type": "typing_update", "typists": typists}
        self.broadcast(conversation_id, payload, sender_ws=sender_ws)


chat_manager = ChatManager()
