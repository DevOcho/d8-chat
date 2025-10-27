# app/routes.py

import datetime
import functools
import json

from flask import Blueprint, g, redirect, render_template, request, session, url_for

from . import sock
from .chat_manager import chat_manager
from .models import (
    Channel,
    ChannelMember,
    Conversation,
    Mention,
    Message,
    Reaction,
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
    user_id = session.get("user_id")
    if user_id is None:
        g.user = None
    else:
        g.user = User.get_or_none(User.id == user_id)


# Decorator to require login for a route.
def login_required(view):
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
    message_ids = [m.id for m in messages]
    all_reactions = (
        Reaction.select(Reaction, User)
        .join(User)
        .where(Reaction.message_id.in_(message_ids))
        .order_by(Reaction.created_at)
    )
    reactions_by_message = {}
    for r in all_reactions:
        mid = r.message_id
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
    from .models import MessageAttachment, UploadedFile

    message_ids = [m.id for m in messages]
    all_links = (
        MessageAttachment.select(MessageAttachment, UploadedFile)
        .join(UploadedFile)
        .where(MessageAttachment.message_id.in_(message_ids))
    )
    for link in all_links:
        mid = link.message_id
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
    channel_conv_ids = [f"channel_{c.id}" for c in user_channels]
    channel_convs_query = Conversation.select().where(
        Conversation.conversation_id_str.in_(channel_conv_ids)
    )
    all_conversations = list(dm_convs_query | channel_convs_query)

    dm_partner_ids = {
        int(uid)
        for conv in all_conversations
        if conv.type == "dm"
        for uid in conv.conversation_id_str.split("_")[1:]
        if int(uid) != g.user.id
    }
    direct_message_users = User.select().where(User.id.in_(list(dm_partner_ids)))

    unread_info = {}
    if all_conversations:
        statuses = UserConversationStatus.select().where(
            UserConversationStatus.user == g.user,
            UserConversationStatus.conversation.in_([c.id for c in all_conversations]),
        )
        last_read_map = {s.conversation.id: s.last_read_timestamp for s in statuses}
        for conv in all_conversations:
            last_read = last_read_map.get(conv.id, datetime.datetime.min)
            if conv.type == "channel":
                mentions = (
                    Mention.select()
                    .join(Message)
                    .where(
                        (Mention.user == g.user)
                        & (Message.conversation == conv)
                        & (Message.created_at > last_read)
                    )
                    .count()
                )
                has_unread = (
                    mentions > 0
                    or Message.select()
                    .where(
                        (Message.conversation == conv)
                        & (Message.created_at > last_read)
                        & (Message.user != g.user)
                    )
                    .exists()
                )
            else:  # DM
                mentions = (
                    Message.select()
                    .where(
                        (Message.conversation_id == conv.id)
                        & (Message.created_at > last_read)
                        & (Message.user != g.user)
                    )
                    .count()
                )
                has_unread = mentions > 0
            unread_info[conv.conversation_id_str] = {
                "mentions": mentions,
                "has_unread": has_unread,
            }

    has_unreads = any(info["has_unread"] for info in unread_info.values())
    last_view_time = g.user.last_threads_view_at or datetime.datetime.min
    user_thread_replies = Message.select().where(
        (Message.user == g.user) & (Message.reply_type == "thread")
    )
    involved_parent_ids = {r.parent_message_id for r in user_thread_replies}
    started_threads = Message.select(Message.id).where(
        (Message.user == g.user) & (Message.last_reply_at.is_null(False))
    )
    involved_parent_ids.update(p.id for p in started_threads)
    has_unread_threads = False
    if involved_parent_ids:
        has_unread_threads = (
            Message.select()
            .where(
                (Message.id.in_(list(involved_parent_ids)))
                & (Message.last_reply_at > last_view_time)
            )
            .exists()
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
    }
    chat_manager.broadcast_to_all(payload)

    try:
        while True:
            data = json.loads(ws.receive())
            event_type = data.get("type")
            conv_id_str = data.get("conversation_id") or getattr(ws, "channel_id", None)

            if event_type == "subscribe":
                if conv_id_str:
                    chat_manager.subscribe(conv_id_str, ws)
                continue

            if event_type in ["typing_start", "typing_stop"]:
                is_typing = event_type == "typing_start"
                chat_manager.handle_typing_event(
                    conversation_id=conv_id_str,
                    user=ws.user,
                    is_typing=is_typing,
                    sender_ws=ws,
                )
                continue

            # --- New Message Handling ---
            chat_text = data.get("chat_message")
            parent_id = data.get("parent_message_id")
            reply_type = data.get("reply_type")
            attachment_file_ids = data.get("attachment_file_ids")
            quoted_message_id = data.get("quoted_message_id")

            if not chat_text and not attachment_file_ids:
                continue

            conversation = Conversation.get_or_none(conversation_id_str=conv_id_str)
            if not conversation:
                continue

            if conversation.type == "channel":
                channel = Channel.get_by_id(
                    conversation.conversation_id_str.split("_")[1]
                )
                if channel.posting_restricted_to_admins:
                    membership = ChannelMember.get_or_none(
                        user=ws.user, channel=channel
                    )
                    if not membership or membership.role != "admin":
                        continue

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
                broadcast_html = ""
                reactions_map_for_reply = get_reactions_for_messages([new_message])
                attachments_map_for_reply = get_attachments_for_messages([new_message])
                new_reply_html = render_template(
                    "partials/message.html",
                    message=new_message,
                    reactions_map=reactions_map_for_reply,
                    attachments_map=attachments_map_for_reply,
                    Message=Message,
                    is_in_thread_view=True,
                )
                broadcast_html += f'<div hx-swap-oob="beforeend:#thread-replies-list-{parent_id}">{new_reply_html}</div>'
                parent_message = Message.get_by_id(parent_id)
                reactions_map_for_parent = get_reactions_for_messages([parent_message])
                attachments_map_for_parent = get_attachments_for_messages(
                    [parent_message]
                )
                parent_in_channel_html = render_template(
                    "partials/message.html",
                    message=parent_message,
                    reactions_map=reactions_map_for_parent,
                    attachments_map=attachments_map_for_parent,
                    Message=Message,
                    is_in_thread_view=False,
                )
                broadcast_html += f'<div id="message-{parent_id}" hx-swap-oob="outerHTML">{parent_in_channel_html}</div>'
                all_participant_ids = {parent_message.user_id}
                replies = Message.select(Message.user_id).where(
                    Message.parent_message == parent_message
                )
                all_participant_ids.update(r.user_id for r in replies)
                unread_link_html = render_template("partials/threads_link_unread.html")
                now = datetime.datetime.now()
                for user_id in all_participant_ids:
                    if user_id == ws.user.id or user_id not in chat_manager.all_clients:
                        continue
                    chat_manager.send_to_user(user_id, unread_link_html)
                    try:
                        status, _ = UserConversationStatus.get_or_create(
                            user_id=user_id, conversation=parent_message.conversation
                        )
                        should_notify = status.last_notified_timestamp is None or (
                            now - status.last_notified_timestamp
                        ) > datetime.timedelta(seconds=10)
                        if should_notify:
                            chat_manager.send_to_user(user_id, {"type": "sound"})
                            status.last_notified_timestamp = now
                            status.save()
                    except Exception as e:
                        print(
                            f"Error sending thread notification to user {user_id}: {e}"
                        )
                chat_manager.broadcast(conv_id_str, broadcast_html, sender_ws=ws)

            else:
                # This is for regular messages and quoted replies.
                reactions_map = get_reactions_for_messages([new_message])
                attachments_map = get_attachments_for_messages([new_message])
                new_message_html = render_template(
                    "partials/message.html",
                    message=new_message,
                    reactions_map=reactions_map,
                    attachments_map=attachments_map,
                    Message=Message,
                )
                message_to_broadcast = f'<div hx-swap-oob="beforeend:#message-list">{new_message_html}</div>'
                chat_manager.broadcast(conv_id_str, message_to_broadcast, sender_ws=ws)

                if new_message.reply_type == "quote":
                    input_html = render_template("partials/chat_input_default.html")
                    reset_payload = f'<div id="chat-input-container" hx-swap-oob="outerHTML">{input_html}</div>'
                    chat_manager.send_to_user(ws.user.id, reset_payload)

            chat_service.send_notifications_for_new_message(new_message, ws.user)
    finally:
        if hasattr(ws, "user") and ws.user:
            user_id = ws.user.id
            chat_manager.set_offline(user_id)
            payload = {
                "type": "presence_update",
                "user_id": user_id,
                "status_class": "presence-away",
            }
            chat_manager.broadcast_to_all(payload)
            chat_manager.unsubscribe(ws)
            print(f"INFO: Client connection closed for '{ws.user.username}'.")
