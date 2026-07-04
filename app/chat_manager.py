# app/chat_manager.py
"""Module for managing WebSocket connections and Redis Pub/Sub."""

# pylint: disable=import-error

import json
import time

import redis
from flask import current_app

from .models import Conversation, UserConversationStatus, utc_now
from .ws_utils import LOCK_TYPES

# Cluster presence is a Redis sorted set: member = user id, score = the unix
# time of that user's most recent heartbeat. A TTL-by-decay model (rather than a
# plain SET) means a crashed/OOM-killed worker can't leave users pinned "online"
# forever — stale scores simply age out, so suppressed pushes self-heal.
PRESENCE_KEY = "presence:online"
PRESENCE_TTL = 90  # a score newer than this many seconds counts as online
PRESENCE_HEARTBEAT_INTERVAL = 30  # how often each worker re-stamps its users
PRESENCE_SWEEP_MAX_AGE = 300  # prune members older than this on heartbeat

# Cap per-user sockets on a single worker so a runaway client can't open
# unbounded connections. When exceeded, the oldest-tracked socket is closed to
# make room for the new one.
MAX_SOCKETS_PER_USER = 10
STATS_LOG_INTERVAL = 60  # seconds between per-worker WS stats log lines


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
        self.sends_ok = 0
        self.sends_failed = 0
        self._last_presence_heartbeat = 0.0
        self._last_stats_log = 0.0

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
                    self._log_stats_maybe()

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
        """Re-stamp presence for this worker's connected users, ~every 30s.

        Driven by the listener loop's 5s wake-up. Refreshing the score keeps
        genuinely-online users online; a disconnected user stops being refreshed
        and ages out. Also opportunistically prunes very old members so the ZSET
        can't grow unbounded from crashed workers.
        """
        if not self.redis_client:
            return
        now = time.time()
        if now - self._last_presence_heartbeat < PRESENCE_HEARTBEAT_INTERVAL:
            return
        self._last_presence_heartbeat = now
        user_ids = list(self.all_clients.keys())
        try:
            if user_ids:
                self.redis_client.zadd(
                    PRESENCE_KEY, {str(uid): now for uid in user_ids}
                )
            self.redis_client.zremrangebyscore(
                PRESENCE_KEY, "-inf", now - PRESENCE_SWEEP_MAX_AGE
            )
        except Exception:  # pylint: disable=broad-exception-caught
            current_app.logger.exception("presence heartbeat failed")

    def _log_stats_maybe(self):
        """Emit one per-worker WS stats line every STATS_LOG_INTERVAL seconds.

        Gives operators a heartbeat of realtime health (socket counts, send
        success/failure, listener restarts) that was previously invisible.
        """
        now = time.time()
        if now - self._last_stats_log < STATS_LOG_INTERVAL:
            return
        self._last_stats_log = now
        current_app.logger.info(
            "ws_stats sockets=%s users=%s sends_ok=%s sends_failed=%s "
            "listener_restarts=%s",
            len(self.clients),
            len(self.all_clients),
            self.sends_ok,
            self.sends_failed,
            self.listener_restarts,
        )

    def _dispatch(self, message):
        """Fan a single pub/sub pmessage out to the matching local clients."""
        channel_name = message["channel"].decode("utf-8")
        payload_data = json.loads(message["data"])

        # Create a safe copy of clients to iterate over for this pod
        clients_on_this_pod = list(self.clients)

        if channel_name.startswith("user:"):
            target_user_id = int(channel_name.split(":", 1)[1])
            # Deliver to every socket this user holds on this worker (multi-tab
            # / web + mobile), applying the active-channel exclusion per socket.
            exclude_channel = payload_data.get("_exclude_channel")
            for ws in list(self.all_clients.get(target_user_id, ())):
                if (
                    exclude_channel
                    and getattr(ws, "channel_id", None) == exclude_channel
                ):
                    continue
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
            self.sends_ok += 1
        except Exception:  # pylint: disable=broad-exception-caught
            self.sends_failed += 1
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
        owner = None
        for uid, socket_set in list(self.all_clients.items()):
            if ws in socket_set:
                owner = uid
                break
        if owner is not None:
            self.set_offline(owner, ws)
        self.unsubscribe(ws)
        self.clients.discard(ws)

    def local_sockets(self, user_id):
        """Snapshot list of this worker's sockets for a user (may be empty)."""
        return list(self.all_clients.get(user_id, ()))

    def send_local(self, user_id, message):
        """Send to all of a user's sockets on THIS worker (no pub/sub).

        Best-effort, same-worker UI nudges (e.g. channel add/remove). Routed
        through _send_message so the per-connection send lock and disconnect
        handling apply.
        """
        for ws in self.local_sockets(user_id):
            self._send_message(ws, message)

    def set_online(self, user_id, ws):
        """Register a websocket for a user (a user may hold several at once —
        multiple tabs, or web + mobile). Marks the user online cluster-wide."""
        existing = self.all_clients.setdefault(user_id, set())
        # Enforce the per-user cap on this worker: evict an existing socket to
        # make room rather than letting a client open unbounded connections.
        # (A backstop against abuse — eviction order isn't significant.)
        while len(existing) >= MAX_SOCKETS_PER_USER:
            victim = next(iter(existing))
            existing.discard(victim)
            self.clients.discard(victim)
            try:
                victim.close(reason=1008, message="Too many connections")
            except Exception:  # pylint: disable=broad-exception-caught
                pass
        self.clients.add(ws)
        self.online_users[user_id] = "online"
        existing.add(ws)
        if self.redis_client:
            # Stamp presence immediately so it's visible cluster-wide without
            # waiting for the next heartbeat.
            try:
                self.redis_client.zadd(PRESENCE_KEY, {str(user_id): time.time()})
            except Exception:  # pylint: disable=broad-exception-caught
                current_app.logger.exception("presence zadd failed on connect")

    def set_offline(self, user_id, ws=None):
        """Deregister a user's socket. The user only goes offline (locally and
        cluster-wide) once their last local socket is gone — closing one of two
        tabs must not stop the other tab from receiving DM badges/sounds.

        ws=None removes the user entirely (all their local sockets), used when a
        full teardown is wanted rather than a single-socket close.

        Returns True if the user is now fully offline on this worker (so the
        caller can decide whether to broadcast a presence-away), False if other
        sockets remain.
        """
        socket_set = self.all_clients.get(user_id)
        if ws is not None and socket_set is not None:
            socket_set.discard(ws)
            self.clients.discard(ws)
            if socket_set:
                return False  # other sockets remain; still online
        else:
            for existing in socket_set or ():
                self.clients.discard(existing)

        self.all_clients.pop(user_id, None)
        self.online_users.pop(user_id, None)
        # Deliberately no ZREM here: another worker may still hold a socket for
        # this user. Their presence score simply stops being refreshed and ages
        # out within PRESENCE_TTL, so a real disconnect self-clears without
        # risking a false-offline while another worker is still serving them.
        return True

    def is_online(self, user_id):
        """Checks if a user is online in the current pod."""
        return user_id in self.online_users

    def is_user_online_in_cluster(self, user_id):
        """True if the user has a fresh presence heartbeat on ANY worker."""
        if self.redis_client:
            try:
                score = self.redis_client.zscore(PRESENCE_KEY, str(user_id))
            except Exception:  # pylint: disable=broad-exception-caught
                return user_id in self.online_users
            if not isinstance(score, (int, float)):
                # Test doubles (Mock) or a missing member: fall back to local.
                return user_id in self.online_users
            return score >= time.time() - PRESENCE_TTL
        return user_id in self.online_users

    def online_user_ids(self):
        """Set of user ids currently online anywhere in the cluster.

        Falls back to this worker's local view when Redis is unavailable or a
        test double returns a non-list (so unit tests can patch online_users).
        """
        if self.redis_client:
            try:
                raw = self.redis_client.zrangebyscore(
                    PRESENCE_KEY, time.time() - PRESENCE_TTL, "+inf"
                )
            except Exception:  # pylint: disable=broad-exception-caught
                return set(self.online_users.keys())
            if not isinstance(raw, (list, tuple, set)):
                return set(self.online_users.keys())
            result = set()
            for member in raw:
                try:
                    result.add(int(member))
                except (TypeError, ValueError):
                    continue
            return result
        return set(self.online_users.keys())

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
