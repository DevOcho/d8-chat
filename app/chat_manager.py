# app/chat_manager.py
import json


class ChatManager:
    def __init__(self):
        self.active_connections = {}
        self.online_users = {}
        self.all_clients = {}
        self.typing_users = {}

    def _send_message(self, ws, message):
        """A robust, centralized method for sending any message (dict or string)."""
        try:
            # If the message is a dictionary, convert it to a JSON string.
            if isinstance(message, dict):
                payload = json.dumps(message)
            else:
                payload = str(message)  # Ensure it's a string

            ws.send(payload)
        except Exception as e:
            print(f"Error sending to client {ws}: {e}")
            self._handle_disconnect(ws)

    def _handle_disconnect(self, ws):
        """Centralized logic to clean up a disconnected client."""
        user_id_to_remove = None
        for uid, client_ws in self.all_clients.items():
            if client_ws == ws:
                user_id_to_remove = uid
                break

        if user_id_to_remove:
            self.set_offline(user_id_to_remove)

        self.unsubscribe(ws)

    def set_online(self, user_id, ws):
        self.online_users[user_id] = "online"
        self.all_clients[user_id] = ws

    def set_offline(self, user_id):
        self.online_users.pop(user_id, None)
        self.all_clients.pop(user_id, None)

    def is_online(self, user_id):
        return user_id in self.online_users

    def broadcast_to_all(self, message):
        for client_ws in list(self.all_clients.values()):
            self._send_message(client_ws, message)

    def subscribe(self, channel_id, ws):
        self.unsubscribe(ws)
        channel_id = str(channel_id)
        if channel_id not in self.active_connections:
            self.active_connections[channel_id] = set()
        self.active_connections[channel_id].add(ws)
        ws.channel_id = channel_id
        print(f"Client {ws} subscribed to channel {channel_id}")

    def unsubscribe(self, ws):
        if hasattr(ws, "channel_id") and ws.channel_id:
            channel_id = ws.channel_id
            if channel_id in self.active_connections:
                self.active_connections[channel_id].discard(ws)
                print(f"Client {ws} unsubscribed from channel {channel_id}")
                if not self.active_connections[channel_id]:
                    del self.active_connections[channel_id]
            ws.channel_id = None
        # When a user unsubscribes (disconnects or changes channels),
        # make sure to stop their typing status in their previous channel.
        user_id = getattr(getattr(ws, "user", None), "id", None)
        username = getattr(getattr(ws, "user", None), "username", None)
        if user_id and username and hasattr(ws, "channel_id") and ws.channel_id:
            self.handle_typing_event(
                ws.channel_id, ws.user, is_typing=False, sender_ws=ws
            )

    def broadcast(self, channel_id, message, sender_ws=None, is_event=False):
        clients_to_send_to = []
        if channel_id:
            channel_id = str(channel_id)
            if channel_id in self.active_connections:
                clients_to_send_to = list(self.active_connections[channel_id])
        elif is_event:
            clients_to_send_to = self.all_clients.values()

        for client_ws in clients_to_send_to:
            if client_ws != sender_ws:
                self._send_message(client_ws, message)

    def handle_typing_event(self, conversation_id, user, is_typing, sender_ws):
        """Manages the state of typing users in a conversation and broadcasts updates."""
        if not conversation_id or not user:
            return

        # Ensure a set exists for the conversation
        self.typing_users.setdefault(conversation_id, set())

        if is_typing:
            self.typing_users[conversation_id].add(user.username)
        else:
            self.typing_users[conversation_id].discard(user.username)

        # Get the current list of typists
        typists = list(self.typing_users.get(conversation_id, []))

        # Prepare the JSON payload to broadcast
        payload = {"type": "typing_update", "typists": typists}

        # Broadcast the new list to everyone in the channel (except the sender)
        self.broadcast(conversation_id, payload, sender_ws=sender_ws)

    def send_to_user(self, user_id, message):
        """Sends a direct message to a specific user if they are online."""
        if user_id in self.all_clients:
            ws = self.all_clients[user_id]
            self._send_message(ws, message)


chat_manager = ChatManager()
