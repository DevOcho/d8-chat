# app/routes.py
"""Main routing and WebSocket handlers for the chat application."""

# pylint: disable=cyclic-import
import datetime
import functools
import json
import time

from flask import (
    Blueprint,
    current_app,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from peewee import JOIN, fn

from . import limiter, sock
from .access import user_has_conversation_access
from .background import spawn_background
from .chat_manager import chat_manager
from .conversation_id import parse_conversation_id
from .htmx_oob import oob_to_selector
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
    db,
    utc_now,
)
from .services import chat_service
from .ws_utils import harden_ws

# This blueprint now only handles the main chat interface and WebSocket.
main_bp = Blueprint("main", __name__)

# Constants shared across blueprints can live here.
PAGE_SIZE = 30
AVATAR_SIZE = (256, 256)


# This function runs before every request to load the logged-in user.
@main_bp.before_app_request
def load_logged_in_user():
    """
    Loads the user from the session into the Flask g object.

    Resolves through ``User.get_active_by_id`` so that deactivated users with
    a still-valid session cookie are treated as logged out — every protected
    route already gates on ``g.user`` being truthy.
    """
    g.user = User.get_active_by_id(session.get("user_id"))


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
                "file_id": link.attachment.id,
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

    # Show a DM in the sidebar when it has had a message in the last 30 days,
    # or has unread messages. There is deliberately NO cap on the count: the
    # previous limit(15) silently hid DMs for anyone with more than 15
    # conversations (and ranked them by UserConversationStatus.updated_at,
    # which is only bumped as a side effect of notifying an *online* recipient
    # — so it was a flaky recency signal too). Basing "recent" on actual
    # Message.created_at makes stale conversations drop off reliably while
    # keeping every active one visible.
    dm_conversation_ids = [c.id for c in all_conversations if c.type == "dm"]
    activity_cutoff = utc_now() - datetime.timedelta(days=30)
    # Most recent message timestamp per DM conversation, in one bulk query. This
    # drives both visibility (recent within 30 days) and sidebar ordering.
    dm_last_activity = dict()
    if dm_conversation_ids:
        for row in (
            Message.select(
                Message.conversation, fn.MAX(Message.created_at).alias("last_at")
            )
            .where(Message.conversation.in_(dm_conversation_ids))
            .group_by(Message.conversation)
        ):
            dm_last_activity[row.conversation_id] = row.last_at

    # partner_id -> last activity time, for the DMs that should be visible.
    visible_dm_partners = dict()
    for conv in all_conversations:
        if conv.type == "dm":
            last_at = dm_last_activity.get(conv.id)
            is_recent = last_at is not None and last_at >= activity_cutoff
            has_unread_msg = unread_info.get(conv.conversation_id_str, dict()).get(
                "has_unread", False
            )

            if is_recent or has_unread_msg:
                try:
                    user_ids = parse_conversation_id(conv.conversation_id_str).user_ids
                except ValueError:
                    continue
                for uid in user_ids:
                    if uid != g.user.id:
                        visible_dm_partners[uid] = last_at

    # Order the sidebar by most-recent conversation first (a DM with no message
    # yet — only possible for a stale/unread edge case — sorts to the bottom).
    partner_users = User.select().where(User.id.in_(list(visible_dm_partners.keys())))
    direct_message_users = sorted(
        partner_users,
        key=lambda u: visible_dm_partners.get(u.id) or datetime.datetime.min,
        reverse=True,
    )

    workspace_member = WorkspaceMember.get_or_none(user=g.user)

    return render_template(
        "chat.html",
        channels=user_channels,
        direct_message_users=direct_message_users,
        # Cluster-wide presence (a set of online user ids) so the sidebar dots
        # reflect users connected to any worker, not just this one.
        online_users=chat_manager.online_user_ids(),
        unread_info=unread_info,
        has_unreads=has_unreads,
        has_unread_threads=has_unread_threads,
        theme=g.user.theme,
        workspace_member=workspace_member,
    )


CATCHUP_LIMIT = 100


@main_bp.route("/chat/conversations/<conv_id_str>/messages/since/<int:last_id>")
@login_required
def catch_up_messages(conv_id_str, last_id):
    """Backfill messages newer than ``last_id`` as OOB appends to #message-list.

    The web client calls this right after a WebSocket reconnect to recover
    anything broadcast while the socket was down — pub/sub is at-most-once, so
    those frames are otherwise lost until a full page refresh. Capped at
    CATCHUP_LIMIT; if more exist, the ``X-D8-Catchup: truncated`` header tells
    the client to reload the whole pane instead of appending a partial gap.
    """
    conversation = Conversation.get_or_none(conversation_id_str=conv_id_str)
    if not conversation:
        return "", 404
    try:
        parsed = parse_conversation_id(conv_id_str)
    except ValueError:
        return "", 400
    if not user_has_conversation_access(g.user, parsed):
        return "", 403

    messages = list(
        Message.select()
        .where((Message.conversation == conversation) & (Message.id > last_id))
        .order_by(Message.id.asc())
        .limit(CATCHUP_LIMIT + 1)
    )
    truncated = len(messages) > CATCHUP_LIMIT
    messages = messages[:CATCHUP_LIMIT]

    reactions_map = get_reactions_for_messages(messages)
    attachments_map = get_attachments_for_messages(messages)
    parts = []
    for m in messages:
        msg_html = render_template(
            "partials/message.html",
            message=m,
            reactions_map=reactions_map,
            attachments_map=attachments_map,
            Message=Message,
        )
        parts.append(oob_to_selector("beforeend", "#message-list", msg_html))

    resp = make_response("".join(parts))
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    if truncated:
        resp.headers["X-D8-Catchup"] = "truncated"
    return resp


@main_bp.route("/chat/sidebar/unreads")
@login_required
def catch_up_sidebar():
    """Re-emit unread-badge OOB fragments for the user's conversations.

    Called by the client after a reconnect so sidebar badges reflect anything
    that changed while the socket was down. Reuses the same bulk unread
    computation as the initial page render.
    """
    user_channels = list(
        Channel.select().join(ChannelMember).where(ChannelMember.user == g.user)
    )
    channel_map = {f"channel_{c.id}": c for c in user_channels}
    dm_convs = list(
        Conversation.select()
        .join(UserConversationStatus)
        .where((UserConversationStatus.user == g.user) & (Conversation.type == "dm"))
    )
    channel_convs = list(
        Conversation.select().where(
            Conversation.conversation_id_str.in_(list(channel_map.keys()))
        )
    )
    all_conversations = dm_convs + channel_convs
    unread_info = _get_unread_info(all_conversations)

    parts = []
    for conv in all_conversations:
        info = unread_info.get(conv.conversation_id_str, {})
        if not info.get("has_unread"):
            continue

        if conv.type == "channel":
            channel = channel_map.get(conv.conversation_id_str)
            if not channel:
                continue
            link_text = f"# {channel.name}"
            hx_get_url = url_for("channels.get_channel_chat", channel_id=channel.id)
            mentions = info.get("mentions", 0)
            template = (
                "partials/unread_badge.html"
                if mentions > 0
                else "partials/bold_link.html"
            )
            parts.append(
                render_template(
                    template,
                    conv_id_str=conv.conversation_id_str,
                    count=mentions,
                    link_text=link_text,
                    hx_get_url=hx_get_url,
                )
            )
        else:  # DM
            try:
                user_ids = parse_conversation_id(conv.conversation_id_str).user_ids
            except ValueError:
                continue
            partner_id = next((uid for uid in user_ids if uid != g.user.id), None)
            partner = User.get_or_none(id=partner_id) if partner_id else None
            if not partner:
                continue
            parts.append(
                render_template(
                    "partials/unread_badge.html",
                    conv_id_str=conv.conversation_id_str,
                    count=info.get("mentions", 0) or 1,
                    link_text=partner.display_name or partner.username,
                    hx_get_url=url_for("dms.get_dm_chat", other_user_id=partner_id),
                )
            )

    resp = make_response("".join(parts))
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


@main_bp.route("/healthz")
@limiter.exempt
def healthz():
    """Realtime-aware health check for the readiness probe.

    Healthy (200) only when Redis is reachable, the pub/sub listener has stamped
    a heartbeat recently, and a trivial DB query works. Reports 503 otherwise so
    a worker whose listener has wedged is pulled from the load balancer. The
    listener self-heals, so this is deliberately NOT wired to a liveness probe
    (a Redis blip shouldn't restart-loop the pods).
    """
    status = {"redis": "unknown", "listener_age_s": None, "db": "unknown"}
    ok = True

    try:
        if chat_manager.redis_client:
            chat_manager.redis_client.ping()
            status["redis"] = "ok"
        else:
            status["redis"] = "absent"
    except Exception:  # pylint: disable=broad-exception-caught
        status["redis"] = "fail"
        ok = False

    hb = chat_manager.listener_heartbeat
    if hb:
        age = time.time() - hb
        status["listener_age_s"] = round(age, 1)
        if age > 60:
            ok = False
    # hb == 0 means the listener hasn't started stamping yet (fresh worker);
    # don't fail readiness on that or the pod could never come up.

    try:
        _ws_db_connect()
        db.execute_sql("SELECT 1")
        status["db"] = "ok"
    except Exception:  # pylint: disable=broad-exception-caught
        status["db"] = "fail"
        ok = False
    finally:
        _ws_db_close()

    return jsonify(status), (200 if ok else 503)


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


def _notify_all_thread_participants(sender, parent_message, conv_id_str):
    """Gathers all thread participants and sends them unread notifications."""
    all_participant_ids = {parent_message.user.id}
    replies = list(
        Message.select(Message.user).where(Message.parent_message == parent_message)
    )
    all_participant_ids.update(r.user.id for r in replies)

    unread_link_html = render_template("partials/threads_link_unread.html")
    now = utc_now()

    for user_id in list(all_participant_ids):
        if user_id == sender.id or not chat_manager.is_user_online_in_cluster(user_id):
            continue
        chat_manager.send_to_user(
            user_id, unread_link_html, exclude_channel=conv_id_str
        )
        try:
            _notify_thread_participant(
                user_id, parent_message.conversation, now, conv_id_str
            )
        except Exception:  # pylint: disable=broad-exception-caught
            current_app.logger.exception(
                f"Error sending thread notification to user {user_id}"
            )


def _broadcast_thread_reply(sender, new_message, parent_id, conv_id_str):
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
    broadcast_html = oob_to_selector(
        "beforeend", f"#thread-replies-list-{int(parent_id)}", new_reply_html
    )

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
    _notify_all_thread_participants(sender, parent_message, conv_id_str)

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

    # sender_ws=None: messages never opt out of echoing to their sender (unlike
    # typing events), so no _exclude_sender is needed. The sender sees their own
    # message via the normal fan-out to whichever sockets are subscribed.
    chat_manager.broadcast(
        conv_id_str, {"_raw_html": broadcast_html, "api_data": api_data}
    )


def _broadcast_regular_message(sender, new_message, conv_id_str):
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
    message_to_broadcast = oob_to_selector(
        "beforeend", "#message-list", new_message_html
    )

    api_data = {
        "type": "new_message",
        "data": serialize_message(new_message, reactions_map, attachments_map),
    }

    chat_manager.broadcast(
        conv_id_str,
        {"_raw_html": message_to_broadcast, "api_data": api_data},
    )
    # The web client resets its own composer on the POST's htmx:afterRequest
    # (including reverting a quote-reply input to the default), so there's no
    # server-pushed input reset here anymore.


# Token-bucket parameters for per-connection WS event rate limiting: a 60-event
# burst that refills at 6/sec (≈60 events / 10s sustained).
_WS_RATE_CAPACITY = 60.0
_WS_RATE_REFILL_PER_SEC = 6.0


def _ws_rate_ok(ws):
    """Per-connection token bucket. Returns False when the client is over its
    event budget. Tolerant of test Mock sockets (which have no real counters)."""
    now = time.time()
    tokens = getattr(ws, "_rate_tokens", None)
    if not isinstance(tokens, (int, float)):
        tokens = _WS_RATE_CAPACITY
    last = getattr(ws, "_rate_last", None)
    if not isinstance(last, (int, float)):
        last = now
    tokens = min(_WS_RATE_CAPACITY, tokens + (now - last) * _WS_RATE_REFILL_PER_SEC)
    if tokens < 1.0:
        ws._rate_tokens = tokens
        ws._rate_last = now
        return False
    ws._rate_tokens = tokens - 1.0
    ws._rate_last = now
    return True


def _safe_handle_frame(ws, raw):
    """Process one inbound WS frame without ever letting a bad frame kill the
    connection.

    Previously the receive loops ran ``json.loads`` + ``_process_ws_event``
    bare, so a single malformed frame or a transient DB hiccup unwound the loop
    and closed the socket (the client then reconnected, resubscribed, and could
    lose messages in the gap). Here we swallow decode errors and unexpected
    handler exceptions — logging them — while letting ``ConnectionClosed``
    propagate so flask-sock can tear down a genuinely closed socket.
    """
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        current_app.logger.warning(
            "WS frame ignored: invalid JSON from user %s",
            getattr(getattr(ws, "user", None), "id", None),
        )
        return
    if not isinstance(data, dict):
        return

    # Per-connection rate limit (flask-limiter only covers HTTP). Drop — but
    # don't close — frames once a client exceeds the bucket, so a chatty or
    # buggy client can't flood the worker.
    if not _ws_rate_ok(ws):
        current_app.logger.warning(
            "WS frame rate-limited for user %s",
            getattr(getattr(ws, "user", None), "id", None),
        )
        return

    # API clients send {"type": "send_message", "content": "..."}; normalize to
    # the internal field name so both routes share one handler.
    if data.get("type") == "send_message" and "content" in data:
        data.setdefault("chat_message", data.get("content"))

    # Check out a pooled DB connection for the duration of this event and hand
    # it back afterwards. The socket itself no longer holds one (see the WS
    # handlers), so the pool isn't drained by idle connections.
    try:
        _ws_db_connect()
        _process_ws_event(ws, data)
    except Exception:  # pylint: disable=broad-exception-caught
        current_app.logger.exception("WS event failed (connection kept alive)")
    finally:
        _ws_db_close()


def _process_ws_event(ws, data):
    """Processes a single WebSocket event."""
    event_type = data.get("type")
    conv_id_str = data.get("conversation_id") or getattr(ws, "channel_id", None)

    if event_type == "subscribe":
        if not conv_id_str:
            return
        # Authorize the subscription. Without this any authenticated client
        # could subscribe to another user's DM (or a channel they're not in)
        # and receive its live traffic — the same hole the send path closed
        # with user_has_conversation_access, missed on subscribe.
        try:
            parsed = parse_conversation_id(conv_id_str)
        except ValueError:
            return
        if not user_has_conversation_access(ws.user, parsed):
            current_app.logger.warning(
                "WS subscribe blocked: user %s not in %r",
                getattr(ws.user, "id", None),
                conv_id_str,
            )
            return
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

    # --- New Message Handling (shared with the HTTP POST endpoint) ---
    handle_inbound_message(
        sender=ws.user,
        conv_id_str=conv_id_str,
        chat_text=data.get("chat_message"),
        parent_id=data.get("parent_message_id"),
        reply_type=data.get("reply_type"),
        attachment_file_ids=data.get("attachment_file_ids"),
        quoted_message_id=data.get("quoted_message_id"),
    )


def handle_inbound_message(
    sender,
    conv_id_str,
    chat_text,
    parent_id=None,
    reply_type=None,
    attachment_file_ids=None,
    quoted_message_id=None,
):
    """Create, broadcast, and notify for one new message from ``sender``.

    Shared by the WebSocket path (``_process_ws_event``) and the web HTTP POST
    endpoint so both enforce identical access rules and produce identical
    broadcasts. Returns a short status string:

      "ok"              — message created and broadcast
      "empty"           — nothing to send (no text, no attachments)
      "no_conversation" — conv_id_str resolves to no conversation
      "bad_request"     — malformed conversation id
      "forbidden"       — sender isn't a member / can't post here
    """
    if not chat_text and not attachment_file_ids:
        return "empty"

    conversation = Conversation.get_or_none(conversation_id_str=conv_id_str)
    if not conversation:
        # A send that resolves to no conversation is dropped. Historically this
        # happened silently after a reconnect left ws.channel_id unset and the
        # frame carried no conversation_id; log it so recurrences are visible.
        current_app.logger.warning(
            "Send dropped: no conversation for %r (user %s)",
            conv_id_str,
            getattr(sender, "id", None),
        )
        return "no_conversation"

    try:
        parsed_conv = parse_conversation_id(conversation.conversation_id_str)
    except ValueError:
        return "bad_request"

    # Membership gate: any authenticated client could otherwise post into any
    # conversation by naming another conv's id.
    if not user_has_conversation_access(sender, parsed_conv):
        current_app.logger.warning(
            "Send blocked: user %s not in %r", sender.id, conv_id_str
        )
        return "forbidden"

    if conversation.type == "channel":
        channel = Channel.get_by_id(parsed_conv.channel_id)
        if channel.posting_restricted_to_admins:
            membership = ChannelMember.get_or_none(user=sender, channel=channel)
            if not membership or membership.role != "admin":
                return "forbidden"

    new_message = chat_service.handle_new_message(
        sender=sender,
        conversation=conversation,
        chat_text=chat_text,
        parent_id=parent_id,
        reply_type=reply_type,
        attachment_file_ids=attachment_file_ids,
        quoted_message_id=quoted_message_id,
    )

    # --- Broadcast and Notification Logic ---
    if new_message.reply_type == "thread":
        _broadcast_thread_reply(sender, new_message, parent_id, conv_id_str)
    else:
        _broadcast_regular_message(sender, new_message, conv_id_str)

    # Notification fan-out (badges, sounds, desktop + FCM push) runs off the hot
    # path so the sender's send returns immediately and slow FCM HTTP can't
    # block the gevent worker.
    spawn_background(
        chat_service.send_notifications_for_new_message, new_message, sender
    )
    return "ok"


# --- WebSocket connection helpers ---

PRESENCE_CLASS_MAP = {
    "online": "presence-online",
    "away": "presence-away",
    "busy": "presence-busy",
}


def _ws_db_connect():
    """Check out a pooled DB connection for a WS event/setup step.

    No-op under tests, where the app context already holds the single
    in-memory SQLite connection and reconnecting/closing would destroy it.
    """
    if not current_app.testing:
        db.connect(reuse_if_open=True)


def _ws_db_close():
    """Return the WS event/setup DB connection to the pool (skip under tests)."""
    if not current_app.testing and not db.is_closed():
        db.close()


def _broadcast_presence(user_id, status):
    """Broadcast a presence_update for a user to every connected client."""
    chat_manager.broadcast_to_all(
        {
            "type": "presence_update",
            "user_id": user_id,
            "status_class": PRESENCE_CLASS_MAP.get(status, "presence-away"),
            "status": status,
        }
    )


def _setup_ws(ws, user, is_api=False):
    """Shared WS connection setup for both routes.

    Attaches the user, hardens the raw socket (send lock + timeout), registers
    presence, announces it, and then releases the pooled DB connection the
    upgrade request checked out. The receive loop checks out a fresh connection
    per event, so an open socket no longer pins a pool slot for its lifetime.
    """
    ws.user = user
    if is_api:
        ws.is_api_client = True
    harden_ws(ws)
    chat_manager.set_online(user.id, ws)
    _broadcast_presence(user.id, user.presence_status)
    _ws_db_close()


def _teardown_ws(ws, label):
    """Shared WS disconnect cleanup: presence-away, unsubscribe (writes read
    state), and logging. Runs its DB work on a freshly checked-out connection
    since the socket released its own during setup."""
    if not (hasattr(ws, "user") and ws.user):
        return
    user = ws.user
    try:
        _ws_db_connect()
        went_offline = chat_manager.set_offline(user.id, ws)
        # Only announce away when the user's last socket on this worker closed;
        # otherwise a second tab is still connected and they're not away.
        if went_offline:
            _broadcast_presence(user.id, "away")
        chat_manager.unsubscribe(ws)
    finally:
        _ws_db_close()
    current_app.logger.info("%s connection closed for '%s'.", label, user.username)


# --- WebSocket Handler ---
@sock.route("/ws/chat")
def chat(ws):
    """Handles all real-time WebSocket communication."""
    user = User.get_active_by_id(session.get("user_id"))
    if not user:
        ws.close(reason=1008, message="Not authenticated")
        return

    # Origin check to prevent Cross-Site WebSocket Hijacking (CSWSH). The
    # browser supplies the page's Origin in the upgrade handshake; we accept
    # only origins that match the server's own URL root or the request host
    # (to cover local dev like http://localhost:5001 and https://d8-chat.local
    # where Flask's SERVER_NAME may not be set). Anything else is closed.
    origin = request.headers.get("Origin")
    allowed_origin = request.url_root.rstrip("/")
    acceptable = {
        allowed_origin,
        f"http://{request.host}",
        f"https://{request.host}",
    }
    if not origin or origin not in acceptable:
        current_app.logger.warning(
            f"WebSocket connection rejected: origin {origin!r} not in {sorted(acceptable)!r}"
        )
        ws.close(reason=1008, message="Invalid origin")
        return

    _setup_ws(ws, user)

    try:
        while True:
            _safe_handle_frame(ws, ws.receive())
    finally:
        _teardown_ws(ws, "Client")


# --- API JSON WebSocket Handler ---
@sock.route("/ws/api/v1")
def api_ws(ws):
    """Handles JSON WebSocket connections for mobile/API clients."""
    from app.blueprints.api_v1 import verify_api_token

    # Auth token is delivered via the Sec-WebSocket-Protocol upgrade header to
    # keep it out of URLs, access logs, and Referer headers. The client must
    # send TWO comma-separated subprotocol values: the marker "d8_sec" plus the
    # token itself (with or without the "d8_sec_" prefix). The server echoes
    # back "d8_sec" to complete the WebSocket subprotocol negotiation.
    requested = [
        p.strip()
        for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",")
        if p.strip()
    ]
    token = next((p for p in requested if p != "d8_sec"), None)
    if token and token.startswith("d8_sec_"):
        token = token[len("d8_sec_") :]

    user_id = verify_api_token(token) if token else None
    if not user_id:
        ws.close(reason=1008, message="Invalid or missing token")
        return

    user = User.get_active_by_id(user_id)
    if not user:
        ws.close(reason=1008, message="User not found")
        return

    _setup_ws(ws, user, is_api=True)

    try:
        while True:
            # _safe_handle_frame maps the mobile {"type":"send_message",
            # "content": ...} shape onto the internal chat_message field, so
            # both routes share one handler.
            _safe_handle_frame(ws, ws.receive())
    finally:
        _teardown_ws(ws, "API client")
