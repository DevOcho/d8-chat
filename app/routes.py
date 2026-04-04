# app/routes.py
"""Main routing and WebSocket handlers for the chat application."""

# pylint: disable=cyclic-import
import datetime
import functools
import json

from flask import Blueprint, g, redirect, render_template, request, session, url_for
from peewee import JOIN, fn

from . import sock
from .chat_manager import chat_manager
from .models import (
    Channel,
    ChannelMember,
    Conversation,
    Mention,
    Message,
    MessageAttachment,
    Reaction,
    UploadedFile,
    User,
    UserConversationStatus,
    WorkspaceMember,
)
from .services import chat_service

# This blueprint now only handles the main chat interface and WebSocket.
main_bp = Blueprint("main", __name__)

# Constants shared across blueprints can live here.
PAGE_SIZE = 30
AVATAR_SIZE = (256, 256)


# This function runs before every request to load the logged-in user.
@main_bp.before_app_request
def load_logged_in_user():
    """Loads the user from the session into the Flask g object."""
    user_id = session.get("user_id")
    if user_id is None:
        g.user = None
    else:
        g.user = User.get_or_none(User.id == user_id)


# Decorator to require login for a route.
def login_required(view):
    """Decorator to require a logged-in user for routes."""

    @functools.wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            # CORRECTED: Redirects to the new auth blueprint's index route.
            return redirect(url_for("auth.index"))
        return view(**kwargs)

    return wrapped_view


# --- SHARED HELPER FUNCTIONS ---


def get_reactions_for_messages(messages):
    """
    Efficiently fetches and groups reactions for a given list of message objects.
    """
    reactions_map = {}
    if not messages:
        return reactions_map
    message_ids = list(m.id for m in messages)
    all_reactions = (
        Reaction.select(Reaction, User)
        .join(User)
        .where(Reaction.message.in_(message_ids))
        .order_by(Reaction.created_at)
    )
    reactions_by_message = {}
    for r in all_reactions:
        mid = r.message.id
        if mid not in reactions_by_message:
            reactions_by_message[mid] = {}
        if r.emoji not in reactions_by_message[mid]:
            reactions_by_message[mid][r.emoji] = {
                "emoji": r.emoji,
                "count": 0,
                "users": [],
                "reactor_names": [],
            }
        group = reactions_by_message[mid][r.emoji]
        group["count"] += 1
        group["users"].append(r.user.id)
        group["reactor_names"].append(r.user.display_name or r.user.username)
    for mid, emoji_groups in reactions_by_message.items():
        reactions_map[mid] = list(emoji_groups.values())
    return reactions_map


def get_attachments_for_messages(messages):
    """
    Efficiently fetches and groups attachment data for a given list of messages.
    """
    attachments_map = {}
    if not messages:
        return attachments_map

    message_ids = list(m.id for m in messages)
    all_links = (
        MessageAttachment.select(MessageAttachment, UploadedFile)
        .join(UploadedFile)
        .where(MessageAttachment.message.in_(message_ids))
    )
    for link in all_links:
        mid = link.message.id
        if mid not in attachments_map:
            attachments_map[mid] = []
        attachments_map[mid].append(
            {
                "url": link.attachment.url,
                "original_filename": link.attachment.original_filename,
                "mime_type": link.attachment.mime_type,
            }
        )
    return attachments_map


def check_and_get_read_state_oob(current_user, just_read_conversation):
    """
    Checks if a user has other unread messages. If not, returns HTML to
    update the sidebar link to the "read" state.
    """
    has_other_unreads = (
        Message.select()
        .join(Conversation)
        .join(
            UserConversationStatus,
            on=(
                (UserConversationStatus.conversation == Conversation.id)
                & (UserConversationStatus.user == current_user.id)
            ),
        )
        .where(
            (Message.user != current_user)
            & (Message.created_at > UserConversationStatus.last_read_timestamp)
            & (Conversation.id != just_read_conversation.id)
        )
        .exists()
    )
    if not has_other_unreads:
        return render_template("partials/unreads_link_read.html")
    return ""


# --- CORE CHAT INTERFACE AND WEBSOCKET ---


def _get_unread_info(all_conversations):
    """Calculates unread message and mention counts for a list of conversations using bulk queries to avoid N+1 issues."""
    unread_info = dict()
    if not all_conversations:
        return unread_info

    conv_ids = list(c.id for c in all_conversations)
    # Use a safe epoch fallback date for missing conversation statuses
    fallback_date = datetime.datetime(1970, 1, 1)

    # 1. Bulk query for unread messages per conversation
    unread_counts = list(
        Message.select(
            Message.conversation.alias("conv_id"),
            fn.COUNT(Message.id).alias("unread_count"),
        )
        .join(
            UserConversationStatus,
            JOIN.LEFT_OUTER,
            on=(
                (UserConversationStatus.conversation == Message.conversation)
                & (UserConversationStatus.user == g.user)
            ),
        )
        .where(
            (Message.conversation.in_(conv_ids))
            & (Message.user != g.user)
            & (
                Message.created_at
                > fn.COALESCE(UserConversationStatus.last_read_timestamp, fallback_date)
            )
        )
        .group_by(Message.conversation)
        .dicts()
    )

    # 2. Bulk query for explicit mentions in channels
    mention_counts = list(
        Message.select(
            Message.conversation.alias("conv_id"),
            fn.COUNT(Message.id).alias("mention_count"),
        )
        .join(Mention, on=(Mention.message == Message.id))
        .join(
            UserConversationStatus,
            JOIN.LEFT_OUTER,
            on=(
                (UserConversationStatus.conversation == Message.conversation)
                & (UserConversationStatus.user == g.user)
            ),
        )
        .where(
            (Message.conversation.in_(conv_ids))
            & (Mention.user == g.user)
            & (
                Message.created_at
                > fn.COALESCE(UserConversationStatus.last_read_timestamp, fallback_date)
            )
        )
        .group_by(Message.conversation)
        .dicts()
    )

    # Map the results to fast lookup dictionaries
    unread_map = dict()
    for row in unread_counts:
        unread_map[row["conv_id"]] = row["unread_count"]

    mention_map = dict()
    for row in mention_counts:
        mention_map[row["conv_id"]] = row["mention_count"]

    # Assign the grouped counts back to the expected payload format
    for conv in all_conversations:
        has_unread = unread_map.get(conv.id, 0) > 0

        if conv.type == "channel":
            mentions = mention_map.get(conv.id, 0)
        else:  # DM
            mentions = unread_map.get(conv.id, 0)

        unread_info[conv.conversation_id_str] = {
            "mentions": mentions,
            "has_unread": has_unread or (mentions > 0),
        }

    return unread_info


def _has_unread_threads(last_view_time):
    """Checks if the user has any unread thread replies."""
    user_thread_replies = list(
        Message.select().where(
            (Message.user == g.user) & (Message.reply_type == "thread")
        )
    )
    involved_parent_ids = {r.parent_message_id for r in user_thread_replies}
    started_threads = list(
        Message.select(Message.id).where(
            (Message.user == g.user) & (Message.last_reply_at.is_null(False))
        )
    )
    involved_parent_ids.update(p.id for p in started_threads)
    if involved_parent_ids:
        return (
            Message.select()
            .where(
                (Message.id.in_(list(involved_parent_ids)))
                & (Message.last_reply_at > last_view_time)
            )
            .exists()
        )
    return False


@main_bp.route("/chat")
@login_required
def chat_interface():
    """Renders the main chat UI shell."""
    user_channels = (
        Channel.select()
        .join(ChannelMember)
        .where(ChannelMember.user == g.user)
        .order_by(Channel.name)
    )
    dm_convs_query = (
        Conversation.select()
        .join(UserConversationStatus)
        .where((UserConversationStatus.user == g.user) & (Conversation.type == "dm"))
    )
    channel_conv_ids = list(f"channel_{c.id}" for c in user_channels)
    channel_convs_query = Conversation.select().where(
        Conversation.conversation_id_str.in_(channel_conv_ids)
    )
    all_conversations = list(dm_convs_query | channel_convs_query)

    # Get unread info for ALL conversations first to calculate the global badges
    unread_info = _get_unread_info(all_conversations)
    has_unreads = any(info["has_unread"] for info in unread_info.values())
    last_view_time = g.user.last_threads_view_at or datetime.datetime.min
    has_unread_threads = _has_unread_threads(last_view_time)

    # Only show recent DMs or DMs with unread messages in the sidebar
    recent_dm_statuses = (
        UserConversationStatus.select(UserConversationStatus.conversation)
        .join(Conversation)
        .where((UserConversationStatus.user == g.user) & (Conversation.type == "dm"))
        .order_by(UserConversationStatus.updated_at.desc())
        .limit(15)
    )
    recent_dm_ids = list(s.conversation_id for s in recent_dm_statuses)

    visible_dm_partner_ids = set()
    for conv in all_conversations:
        if conv.type == "dm":
            is_recent = conv.id in recent_dm_ids
            has_unread_msg = unread_info.get(conv.conversation_id_str, dict()).get(
                "has_unread", False
            )

            if is_recent or has_unread_msg:
                for uid in conv.conversation_id_str.split("_")[1:]:
                    if int(uid) != g.user.id:
                        visible_dm_partner_ids.add(int(uid))

    # Pass only the filtered list of users to the sidebar template
    direct_message_users = (
        User.select()
        .where(User.id.in_(list(visible_dm_partner_ids)))
        .order_by(User.username)
    )

    workspace_member = WorkspaceMember.get_or_none(user=g.user)

    return render_template(
        "chat.html",
        channels=user_channels,
        direct_message_users=direct_message_users,
        online_users=chat_manager.online_users,
        unread_info=unread_info,
        has_unreads=has_unreads,
        has_unread_threads=has_unread_threads,
        theme=g.user.theme,
        workspace_member=workspace_member,
    )


def _notify_thread_participant(user_id, conversation, now, conv_id_str):
    """Sends sound notification for thread replies if needed."""
    status, _ = UserConversationStatus.get_or_create(
        user_id=user_id, conversation=conversation
    )
    should_notify = status.last_notified_timestamp is None or (
        now - status.last_notified_timestamp
    ) > datetime.timedelta(seconds=10)
    if should_notify:
        chat_manager.send_to_user(
            user_id, {"type": "sound"}, exclude_channel=conv_id_str
        )
        status.last_notified_timestamp = now
        status.save()


def _notify_all_thread_participants(ws, parent_message, conv_id_str):
    """Gathers all thread participants and sends them unread notifications."""
    all_participant_ids = {parent_message.user.id}
    replies = list(
        Message.select(Message.user).where(Message.parent_message == parent_message)
    )
    all_participant_ids.update(r.user.id for r in replies)

    unread_link_html = render_template("partials/threads_link_unread.html")
    now = datetime.datetime.now()

    for user_id in list(all_participant_ids):
        if user_id == ws.user.id or not chat_manager.is_user_online_in_cluster(user_id):
            continue
        chat_manager.send_to_user(
            user_id, unread_link_html, exclude_channel=conv_id_str
        )
        try:
            _notify_thread_participant(
                user_id, parent_message.conversation, now, conv_id_str
            )
        except Exception as e:  # pylint: disable=broad-exception-caught
            print(f"Error sending thread notification to user {user_id}: {e}")


def _broadcast_thread_reply(ws, new_message, parent_id, conv_id_str):
    """Broadcasts a thread reply to relevant users."""
    from app.blueprints.api_v1 import serialize_message

    reactions_map_for_reply = get_reactions_for_messages(list((new_message,)))
    attachments_map_for_reply = get_attachments_for_messages(list((new_message,)))
    new_reply_html = render_template(
        "partials/message.html",
        message=new_message,
        reactions_map=reactions_map_for_reply,
        attachments_map=attachments_map_for_reply,
        Message=Message,
        is_in_thread_view=True,
    )
    broadcast_html = f'<div hx-swap-oob="beforeend:#thread-replies-list-{parent_id}">{new_reply_html}</div>'

    parent_message = Message.get_by_id(parent_id)
    reactions_map_for_parent = get_reactions_for_messages(list((parent_message,)))
    attachments_map_for_parent = get_attachments_for_messages(list((parent_message,)))
    parent_in_channel_html = render_template(
        "partials/message.html",
        message=parent_message,
        reactions_map=reactions_map_for_parent,
        attachments_map=attachments_map_for_parent,
        Message=Message,
        is_in_thread_view=False,
    )

    # Inject hx-swap-oob directly to avoid nested divs with duplicate IDs
    parent_in_channel_oob = parent_in_channel_html.replace(
        f'id="message-{parent_id}"', f'id="message-{parent_id}" hx-swap-oob="true"', 1
    )
    broadcast_html += parent_in_channel_oob

    # Delegate the notification loop to our new helper function
    _notify_all_thread_participants(ws, parent_message, conv_id_str)

    api_data = {
        "type": "new_thread_reply",
        "data": {
            "parent_message": serialize_message(
                parent_message, reactions_map_for_parent, attachments_map_for_parent
            ),
            "reply": serialize_message(
                new_message, reactions_map_for_reply, attachments_map_for_reply
            ),
        },
    }

    chat_manager.broadcast(
        conv_id_str, {"_raw_html": broadcast_html, "api_data": api_data}, sender_ws=ws
    )


def _broadcast_regular_message(ws, new_message, conv_id_str):
    """Broadcasts a regular message or quoted reply."""
    from app.blueprints.api_v1 import serialize_message

    reactions_map = get_reactions_for_messages(list((new_message,)))
    attachments_map = get_attachments_for_messages(list((new_message,)))
    new_message_html = render_template(
        "partials/message.html",
        message=new_message,
        reactions_map=reactions_map,
        attachments_map=attachments_map,
        Message=Message,
    )
    message_to_broadcast = (
        f'<div hx-swap-oob="beforeend:#message-list">{new_message_html}</div>'
    )

    api_data = {
        "type": "new_message",
        "data": serialize_message(new_message, reactions_map, attachments_map),
    }

    chat_manager.broadcast(
        conv_id_str,
        {"_raw_html": message_to_broadcast, "api_data": api_data},
        sender_ws=ws,
    )

    if new_message.reply_type == "quote":
        input_html = render_template("partials/chat_input_default.html")
        reset_payload = {
            "_raw_html": f'<div id="chat-input-container" hx-swap-oob="outerHTML">{input_html}</div>'
        }
        chat_manager.send_to_user(ws.user.id, reset_payload)


def _process_ws_event(ws, data):
    """Processes a single WebSocket event."""
    event_type = data.get("type")
    conv_id_str = data.get("conversation_id") or getattr(ws, "channel_id", None)

    if event_type == "subscribe":
        if conv_id_str:
            chat_manager.subscribe(conv_id_str, ws)
        return

    if event_type in ("typing_start", "typing_stop"):
        is_typing = event_type == "typing_start"
        chat_manager.handle_typing_event(
            conversation_id=conv_id_str,
            user=ws.user,
            is_typing=is_typing,
            sender_ws=ws,
        )
        return

    # --- New Message Handling ---
    chat_text = data.get("chat_message")
    parent_id = data.get("parent_message_id")
    reply_type = data.get("reply_type")
    attachment_file_ids = data.get("attachment_file_ids")
    quoted_message_id = data.get("quoted_message_id")

    if not chat_text and not attachment_file_ids:
        return

    conversation = Conversation.get_or_none(conversation_id_str=conv_id_str)
    if not conversation:
        return

    if conversation.type == "channel":
        channel = Channel.get_by_id(conversation.conversation_id_str.split("_")[1])
        if channel.posting_restricted_to_admins:
            membership = ChannelMember.get_or_none(user=ws.user, channel=channel)
            if not membership or membership.role != "admin":
                return

    new_message = chat_service.handle_new_message(
        sender=ws.user,
        conversation=conversation,
        chat_text=chat_text,
        parent_id=parent_id,
        reply_type=reply_type,
        attachment_file_ids=attachment_file_ids,
        quoted_message_id=quoted_message_id,
    )

    # --- Broadcast and Notification Logic ---
    if new_message.reply_type == "thread":
        _broadcast_thread_reply(ws, new_message, parent_id, conv_id_str)
    else:
        _broadcast_regular_message(ws, new_message, conv_id_str)

    chat_service.send_notifications_for_new_message(new_message, ws.user)


# --- WebSocket Handler ---
@sock.route("/ws/chat")
def chat(ws):
    """Handles all real-time WebSocket communication."""
    user = session.get("user_id") and User.get_or_none(id=session.get("user_id"))
    if not user:
        ws.close(reason=1008, message="Not authenticated")
        return

    # Origin check to prevent CSWSH
    origin = request.headers.get("Origin")
    allowed_origin = request.url_root.rstrip("/")
    # For local dev, Flask may not have the server name configured.
    if "127.0.0.1" in allowed_origin or "localhost" in allowed_origin:
        if origin not in (allowed_origin, f"http://{request.host}"):
            print(
                f"WARN: WebSocket origin '{origin}' doesn't match allowed '{allowed_origin}'. Allowing for local dev."
            )
    elif not origin or origin != allowed_origin:
        print(f"ERROR: WebSocket connection from invalid origin '{origin}'. Closing.")
        ws.close(reason=1008, message="Invalid origin")
        return

    ws.user = user
    chat_manager.set_online(user.id, ws)
    presence_class_map = {
        "online": "presence-online",
        "away": "presence-away",
        "busy": "presence-busy",
    }
    status_class = presence_class_map.get(user.presence_status, "presence-away")
    payload = {
        "type": "presence_update",
        "user_id": user.id,
        "status_class": status_class,
        "status": user.presence_status,
    }
    chat_manager.broadcast_to_all(payload)

    try:
        while True:
            data = json.loads(ws.receive())
            _process_ws_event(ws, data)
    finally:
        if hasattr(ws, "user") and ws.user:
            user_id = ws.user.id
            chat_manager.set_offline(user_id)
            payload = {
                "type": "presence_update",
                "user_id": user_id,
                "status_class": "presence-away",
                "status": "away",
            }
            chat_manager.broadcast_to_all(payload)
            chat_manager.unsubscribe(ws)
            print(f"INFO: Client connection closed for '{ws.user.username}'.")


# --- API JSON WebSocket Handler ---
@sock.route("/ws/api/v1")
def api_ws(ws):
    """Handles JSON WebSocket connections for mobile/API clients."""
    from app.blueprints.api_v1 import verify_api_token

    # We grab the token directly out of the initial connection URL params
    token = request.args.get("token")
    if token and token.startswith("d8_sec_"):
        token = token[7:]

    user_id = verify_api_token(token)
    if not user_id:
        ws.close(reason=1008, message="Invalid or missing token")
        return

    user = User.get_or_none(id=user_id)
    if not user:
        ws.close(reason=1008, message="User not found")
        return

    ws.user = user
    ws.is_api_client = True  # Tell chat_manager to serve raw JSON to this WS

    chat_manager.set_online(user.id, ws)

    presence_class_map = {
        "online": "presence-online",
        "away": "presence-away",
        "busy": "presence-busy",
    }
    status_class = presence_class_map.get(user.presence_status, "presence-away")

    payload = {
        "type": "presence_update",
        "user_id": user.id,
        "status_class": status_class,
        "status": user.presence_status,
    }
    chat_manager.broadcast_to_all(payload)

    try:
        while True:
            data = json.loads(ws.receive())

            # Route API events using the exact same flow but without relying on HTML
            # Mobile app is expected to send {"type": "send_message", "content": "..."}
            event_type = data.get("type")

            if event_type == "send_message":
                data["chat_message"] = data.get("content")

            _process_ws_event(ws, data)

    finally:
        if hasattr(ws, "user") and ws.user:
            user_id = ws.user.id
            chat_manager.set_offline(user_id)
            disconnect_payload = {
                "type": "presence_update",
                "user_id": user_id,
                "status_class": "presence-away",
                "status": "away",
            }
            chat_manager.broadcast_to_all(disconnect_payload)
            chat_manager.unsubscribe(ws)
            print(f"INFO: API Client connection closed for '{ws.user.username}'.")
