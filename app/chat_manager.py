# A simple in-memory manager for WebSocket clients.
# In a production, multi-worker setup, this would be replaced
# with a more robust solution like Redis Pub/Sub.


class ChatManager:
    def __init__(self):
        # {channel_id: {websocket_client, ...}}
        self.active_connections = {}
        # {user_id: 'online'} - tracks presence
        self.online_users = {}
        # {user_id: websocket_client} - for broadcasting to all
        self.all_clients = {}

    def set_online(self, user_id, ws):
        """Marks a user as online and stores their client."""
        self.online_users[user_id] = "online"
        self.all_clients[user_id] = ws

    def set_offline(self, user_id):
        """Marks a user as offline."""
        self.online_users.pop(user_id, None)
        self.all_clients.pop(user_id, None)

    def is_online(self, user_id):
        """Checks if a user is online."""
        return user_id in self.online_users

    def broadcast_to_all(self, message_html):
        """Broadcasts a message to every connected client."""
        for client_ws in self.all_clients.values():
            try:
                client_ws.send(message_html)
            except Exception as e:
                print(f"Error sending presence update: {e}")

    def subscribe(self, channel_id, ws):
        """Subscribes a client to a channel."""
        # Unsubscribe from any previous channel first
        self.unsubscribe(ws)

        channel_id = str(channel_id)  # Ensure key is a string
        if channel_id not in self.active_connections:
            self.active_connections[channel_id] = set()
        self.active_connections[channel_id].add(ws)
        # Store the current channel on the websocket object itself for easy access
        ws.channel_id = channel_id
        print(f"Client {ws} subscribed to channel {channel_id}")

    def unsubscribe(self, ws):
        """Removes a client from any channel it might be subscribed to."""
        # Check if the client was subscribed to a channel
        if hasattr(ws, "channel_id") and ws.channel_id:
            channel_id = ws.channel_id
            if channel_id in self.active_connections:
                self.active_connections[channel_id].discard(ws)
                print(f"Client {ws} unsubscribed from channel {channel_id}")
                # Clean up empty sets
                if not self.active_connections[channel_id]:
                    del self.active_connections[channel_id]
            ws.channel_id = None

    def broadcast(self, channel_id, message_html, sender_ws=None, is_event=False):
        """
        Broadcasts a message to clients.
        If channel_id is provided, sends to that channel.
        If channel_id is None and is_event is True, sends to all clients.
        """
        clients_to_send_to = []

        if channel_id:  # Standard channel broadcast
            channel_id = str(channel_id)
            if channel_id in self.active_connections:
                clients_to_send_to = list(self.active_connections[channel_id])
        elif is_event:  # Global event broadcast
            clients_to_send_to = self.all_clients.values()

        for client_ws in clients_to_send_to:
            if client_ws != sender_ws:
                try:
                    client_ws.send(message_html)
                except Exception as e:
                    print(f"Error sending to client {client_ws}: {e}")
                    self.unsubscribe(client_ws)  # Unsubscribe from channels
                    # We also need to remove them from the global list if they are disconnected
                    user_id_to_remove = None
                    for uid, ws in self.all_clients.items():
                        if ws == client_ws:
                            user_id_to_remove = uid
                            break
                    if user_id_to_remove:
                        self.set_offline(user_id_to_remove)


# Create a single global instance of the manager
chat_manager = ChatManager()
