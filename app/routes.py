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

from . import limiter, sock, warm_markdown_lookups
from .access import (
    accessible_conversations,
    user_can_post,
    user_has_conversation_access,
)
from .background import spawn_background
from .chat_manager import chat_manager
from .conversation_id import parse_conversation_id
from .htmx_oob import message_list_append_oob, oob_by_id, oob_to_selector
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
from .ws_utils import harden_ws, reject_ws

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


def prepare_messages_for_render(messages):
    """Preload what partials/message.html reads, for a whole page of messages.

    Rendering touched the author, the author's avatar, the conversation, the
    poll, the thread reply count and any quoted message lazily — about 8
    queries per message, so opening a 30-message channel ran ~220. This runs a
    fixed handful, attaches the results to each message, and returns the
    ``(reactions_map, attachments_map)`` the templates take.
    """
    # pylint: disable=import-outside-toplevel
    from .models import Poll

    messages = [m for m in messages if m is not None]
    if not messages:
        return {}, {}
    ids = [m.id for m in messages]

    ref_ids = {m.quoted_message_id for m in messages if m.quoted_message_id}
    ref_ids |= {
        m.parent_message_id
        for m in messages
        if m.parent_message_id and m.reply_type == "quote"
    }
    refs = (
        {r.id: r for r in Message.select().where(Message.id.in_(list(ref_ids)))}
        if ref_ids
        else {}
    )

    user_ids = {m.user_id for m in messages if m.user_id}
    user_ids |= {r.user_id for r in refs.values() if r.user_id}
    users = {}
    if user_ids:
        users = {
            u.id: u
            for u in User.select(User, UploadedFile)
            .join(UploadedFile, JOIN.LEFT_OUTER, on=(User.avatar == UploadedFile.id))
            .where(User.id.in_(list(user_ids)))
        }
    conversations = {
        c.id: c
        for c in Conversation.select().where(
            Conversation.id.in_(list({m.conversation_id for m in messages}))
        )
    }
    polls = {p.message_id: p for p in Poll.select().where(Poll.message.in_(ids))}
    reply_counts = {
        row.parent_message_id: row.n
        for row in Message.select(
            Message.parent_message, fn.COUNT(Message.id).alias("n")
        )
        .where((Message.parent_message.in_(ids)) & (Message.reply_type == "thread"))
        .group_by(Message.parent_message)
    }

    warm_markdown_lookups(
        [m.content for m in messages] + [r.content for r in refs.values()]
    )

    for ref in refs.values():
        if ref.user_id in users:
            ref.user = users[ref.user_id]
    for m in messages:
        if m.user_id in users:
            m.user = users[m.user_id]
        if m.conversation_id in conversations:
            m.conversation = conversations[m.conversation_id]
        if m.quoted_message_id in refs:
            m.quoted_message = refs[m.quoted_message_id]
        if m.reply_type == "quote" and m.parent_message_id in refs:
            m.parent_message = refs[m.parent_message_id]
        m._poll_cache = polls.get(m.id)  # pylint: disable=protected-access
        m._thread_reply_count = reply_counts.get(m.id, 0)  # pylint: disable=protected-access

    return get_reactions_for_messages(messages), get_attachments_for_messages(messages)


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
        mid = link.message_id
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


# Consecutive messages from one author within this window (same day, nobody
# else in between) render as a single visual group: one avatar/name/timestamp
# header with the follow-ups tucked underneath, Slack-style.
MESSAGE_GROUP_WINDOW = datetime.timedelta(minutes=5)


def annotate_message_grouping(messages, prev_message=None):
    """
    Set ``message.same_sender`` on each message so the templates can group a run
    of messages from one author under a single header.

    A message groups with the one above it when they share an author, fall on
    the same calendar day, and were sent within ``MESSAGE_GROUP_WINDOW`` — with
    no other author or system message in between. Thread replies are skipped
    because they aren't rendered inline, so the message after a thread reply
    still groups with the last *visible* message.

    ``prev_message`` is the message immediately above this batch (a pagination
    cursor or the last message already on screen) so grouping stays correct
    across a fetch/broadcast boundary; pass ``None`` when nothing precedes it.
    """
    prev = prev_message
    for message in messages:
        if message.reply_type == "thread":
            message.same_sender = False
            continue  # not rendered inline — don't let it anchor/break a group
        message.same_sender = bool(
            prev is not None
            and message.user_id is not None
            and prev.user_id is not None
            and prev.user_id == message.user_id
            and message.created_at.date() == prev.created_at.date()
            and message.created_at - prev.created_at <= MESSAGE_GROUP_WINDOW
        )
        prev = message
    return messages


def previous_visible_message(new_message):
    """
    The most recent non-thread message before ``new_message`` in its
    conversation — the one that sits directly above it in the main flow. Used
    to compute grouping for realtime/catch-up renders that don't have the
    surrounding list in hand.
    """
    return (
        Message.select()
        .where(
            (Message.conversation == new_message.conversation)
            & (Message.id < new_message.id)
            & Message.in_timeline()
        )
        .order_by(Message.id.desc())
        .first()
    )


def date_separator_oob(message, prev_message, conv_id_str):
    """
    OOB append of a date divider above ``message`` when it opens a new day.

    The page templates emit a divider whenever a row's calendar day differs from
    the row above it. Realtime appends render one message at a time with no list
    in hand, so the same check happens here against ``prev_message`` (the message
    directly above it in the flow — see ``previous_visible_message``). Without
    this, a day's first message only grows its divider on the next full reload.

    Returns "" when the day hasn't changed or ``message`` isn't rendered inline.
    """
    if message.reply_type == "thread":
        return ""
    if (
        prev_message is not None
        and prev_message.created_at.date() == message.created_at.date()
    ):
        return ""
    return message_list_append_oob(
        conv_id_str,
        render_template("partials/date_separator.html", date_obj=message.created_at),
    )


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
            & Message.in_timeline()
            & Conversation.id.in_(accessible_conversations(current_user))
        )
        .exists()
    )
    if not has_other_unreads:
        return render_template("partials/unreads_link_read.html")
    return ""


def had_unread_messages(user, conversation, last_read_timestamp):
    """Whether ``conversation`` had messages from others after ``last_read_timestamp``."""
    return (
        Message.select()
        .where(
            (Message.conversation == conversation)
            & (Message.created_at > last_read_timestamp)
            & (Message.user != user)
            & Message.in_timeline()
        )
        .exists()
    )


def _clear_badge_html(user, conversation):
    """The web OOB swap that clears ``conversation``'s badge for ``user``."""
    conv_id_str = conversation.conversation_id_str
    parsed = parse_conversation_id(conv_id_str)
    if parsed.type == "channel":
        channel = Channel.get_or_none(Channel.id == parsed.channel_id)
        if channel is None:
            return ""
        hx_get_url = url_for("channels.get_channel_chat", channel_id=channel.id)
        link_text = f"# {channel.name}"
    else:
        other_ids = [uid for uid in parsed.user_ids if uid != user.id] or [user.id]
        other = User.get_or_none(User.id == other_ids[0])
        if other is None:
            return ""
        hx_get_url = url_for("dms.get_dm_chat", other_user_id=other.id)
        link_text = other.display_name or other.username
    return render_template(
        "partials/clear_badge.html",
        conv_id_str=conv_id_str,
        hx_get_url=hx_get_url,
        link_text=link_text,
    )


def broadcast_conversation_read(user, conversation, web_html=None):
    """Clear a just-read conversation's unread state on all of ``user``'s clients.

    Web tabs get the badge-clearing OOB swap (plus the "Unreads" link going
    quiet when nothing else is unread); mobile sockets get an
    ``unread_updated`` frame with a zero count. Without this, reading on one
    device left the badge up on every other one. Callers that already
    rendered that HTML for their own response pass it as ``web_html``.

    Best effort: the read itself is already saved, so a Redis outage must not
    turn it into an error.
    """
    if web_html is None:
        web_html = _clear_badge_html(user, conversation) + (
            check_and_get_read_state_oob(user, conversation)
        )
    try:
        chat_manager.send_to_user(
            user.id,
            {
                "_raw_html": web_html,
                "api_data": {
                    "type": "unread_updated",
                    "data": {
                        "conversation_id_str": conversation.conversation_id_str,
                        "unread_count": 0,
                        "is_mention": False,
                    },
                },
            },
        )
    except Exception:  # pylint: disable=broad-exception-caught
        current_app.logger.exception(
            "read-state broadcast for %s failed", conversation.conversation_id_str
        )


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
            & Message.in_timeline()
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
    """Checks if the user has any unread thread replies: a thread they started
    or replied in has had a reply since they last opened Threads.

    One EXISTS. It used to load every thread reply the user had ever written,
    as full rows, on every page load.
    """
    replied_in = Message.select(Message.parent_message).where(
        (Message.user == g.user) & (Message.reply_type == "thread")
    )
    return (
        Message.select()
        .where(
            (Message.last_reply_at > last_view_time)
            & ((Message.user == g.user) | Message.id.in_(replied_in))
        )
        .exists()
    )


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
        # reflect users connected to any worker, not just this one. active_users
        # additionally require a recent client activity ping, so an idle user who
        # left a tab open renders as "away" rather than a stale green dot.
        online_users=chat_manager.online_user_ids(),
        active_users=chat_manager.active_user_ids(),
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
        .where(
            (Message.conversation == conversation)
            & (Message.id > last_id)
            & Message.in_timeline()
        )
        .order_by(Message.id.asc())
        .limit(CATCHUP_LIMIT + 1)
    )
    truncated = len(messages) > CATCHUP_LIMIT
    messages = messages[:CATCHUP_LIMIT]

    reactions_map, attachments_map = prepare_messages_for_render(messages)
    # Continue grouping from the last message the client already has on screen.
    prev = previous_visible_message(messages[0]) if messages else None
    if messages:
        annotate_message_grouping(messages, prev_message=prev)
    parts = []
    for m in messages:
        # Same day-change check the page render does, carried across the gap the
        # client is backfilling.
        parts.append(date_separator_oob(m, prev, conv_id_str))
        msg_html = render_template(
            "partials/message.html",
            message=m,
            reactions_map=reactions_map,
            attachments_map=attachments_map,
            Message=Message,
        )
        parts.append(message_list_append_oob(conv_id_str, msg_html))
        if m.reply_type != "thread":
            prev = m

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
            # Materialize the sidebar row before badging it: a DM created
            # while this client's socket was down has no row in its DOM, so
            # the badge's OOB target wouldn't exist and the conversation
            # would stay invisible until a full page reload. The client
            # drops the row if it already has one (htmx:oobBeforeSwap
            # dedupe in chat.js).
            parts.append(
                render_template(
                    "partials/dm_list_item_oob.html",
                    user=partner,
                    conv_id_str=conv.conversation_id_str,
                    is_online=chat_manager.is_user_online_in_cluster(partner.id),
                    is_active=chat_manager.is_user_active(partner.id),
                )
            )
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


def _notify_all_thread_participants(sender, parent_message, conv_id_str):
    """Light up the Threads link (and ring, at most every 10s) for everyone in
    the thread who's online.

    Runs off the send path (``spawn_background``). It used to run inline,
    loading one User per reply and doing a get_or_create plus a save per
    participant: ~60 queries before a 5-reply thread's sender got a response.
    """
    participant_ids = {parent_message.user_id}
    participant_ids.update(
        r.user_id
        for r in Message.select(Message.user)
        .where(
            (Message.parent_message == parent_message)
            & (Message.reply_type == "thread")
        )
        .distinct()
    )
    online = chat_manager.online_user_ids()
    targets = [
        uid
        for uid in participant_ids
        if uid is not None and uid != sender.id and uid in online
    ]
    if not targets:
        return

    conversation = parent_message.conversation
    statuses = {
        s.user_id: s
        for s in UserConversationStatus.select().where(
            (UserConversationStatus.conversation == conversation)
            & (UserConversationStatus.user.in_(targets))
        )
    }
    unread_link_html = render_template("partials/threads_link_unread.html")
    now = utc_now()
    ring = []
    for user_id in targets:
        try:
            chat_manager.send_to_user(
                user_id, unread_link_html, exclude_channel=conv_id_str
            )
            status = statuses.get(user_id)
            last = status.last_notified_timestamp if status else None
            if last is None or (now - last) > datetime.timedelta(seconds=10):
                chat_manager.send_to_user(
                    user_id, {"type": "sound"}, exclude_channel=conv_id_str
                )
                ring.append(user_id)
        except Exception:  # pylint: disable=broad-exception-caught
            current_app.logger.exception(
                f"Error sending thread notification to user {user_id}"
            )
    if ring:
        # Targeted UPDATE (see chat_service._mark_notified). Participants
        # without a status row get no stamp; they'll ring again next time.
        UserConversationStatus.update(last_notified_timestamp=now).where(
            (UserConversationStatus.conversation == conversation)
            & (UserConversationStatus.user.in_(ring))
        ).execute()


THREAD_PANEL_SELECTOR = "#right-panel-offcanvas"


def message_oob_html(
    message, reactions_map, attachments_map, *, is_in_thread_view, selector
):
    """``message.html`` as an OOB outerHTML swap of the copies matching ``selector``.

    The swap attribute goes on the message element itself, so it replaces the
    target with no wrapper div. Prefer this to a bare id swap
    (``hx-swap-oob="true"``): htmx replaces *every* element with the id, so a
    thread parent shown both in the channel and in the thread panel got one
    rendering in both places, and the two differ (reply indicator, toolbar).
    """
    html = render_template(
        "partials/message.html",
        message=message,
        reactions_map=reactions_map,
        attachments_map=attachments_map,
        Message=Message,
        is_in_thread_view=is_in_thread_view,
    )
    return html.replace(
        f'id="message-{message.id}"',
        f'id="message-{message.id}" hx-swap-oob="outerHTML:{selector}"',
        1,
    )


def delete_message_and_broadcast(message):
    """Delete ``message`` (see ``chat_service.delete_message_and_files``) and
    tell every client. Deleting the last reply under a deleted thread parent
    removes the parent's placeholder too."""
    conv_id_str = message.conversation.conversation_id_str
    parent_id = message.parent_message_id if message.reply_type == "thread" else None
    placeholder = chat_service.delete_message_and_files(message)
    broadcast_message_removed(message, conv_id_str, placeholder)

    parent = Message.get_or_none(id=parent_id) if parent_id else None
    if parent is not None and parent.is_deleted:
        has_replies = (
            Message.select()
            .where(
                (Message.parent_message == parent) & (Message.reply_type == "thread")
            )
            .exists()
        )
        if not has_replies:
            chat_service.delete_message_and_files(parent)
            broadcast_message_removed(parent, conv_id_str, False)


def broadcast_message_removed(message, conv_id_str, placeholder):
    """Tell every client in ``conv_id_str`` that ``message`` is gone.

    ``placeholder`` (from ``chat_service.delete_message_and_files``) means the
    message started a thread and now renders as "This message was deleted.":
    web copies are re-rendered in place and mobile gets ``message_edited``
    with ``is_deleted``. Otherwise every copy is removed (``message_deleted``).
    """
    # pylint: disable=import-outside-toplevel
    from app.blueprints.api_v1 import serialize_message

    if placeholder:
        message = Message.get_by_id(message.id)
        html = message_update_oob(message, {}, {})
        api_data = {
            "type": "message_edited",
            "data": serialize_message(message, {}, {}),
        }
    else:
        html = oob_by_id(f"message-{int(message.id)}", "delete")
        api_data = {
            "type": "message_deleted",
            "data": {"message_id": message.id, "conversation_id_str": conv_id_str},
        }
    chat_manager.broadcast(conv_id_str, {"_raw_html": html, "api_data": api_data})


def message_update_oob(message, reactions_map, attachments_map):
    """OOB HTML refreshing every on-page copy of an updated ``message``.

    The channel copy gets the channel rendering. The thread panel's copy
    (thread replies, and a thread parent shown at the top of its panel) gets
    the thread rendering, sent only when the message is part of a thread, so
    ordinary edits don't make htmx log a missing target.
    """
    in_thread = message.reply_type == "thread"
    parts = []
    if not in_thread:
        parts.append(
            message_oob_html(
                message,
                reactions_map,
                attachments_map,
                is_in_thread_view=False,
                selector=f"#message-list #message-{message.id}",
            )
        )
    if in_thread or message.replies.where(Message.reply_type == "thread").exists():
        parts.append(
            message_oob_html(
                message,
                reactions_map,
                attachments_map,
                is_in_thread_view=True,
                selector=f"{THREAD_PANEL_SELECTOR} #message-{message.id}",
            )
        )
    return "".join(parts)


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
    # Only the channel copy shows the reply count; leave the thread panel's
    # copy of the parent alone (a bare id swap replaced it with this one).
    broadcast_html += message_oob_html(
        parent_message,
        reactions_map_for_parent,
        attachments_map_for_parent,
        is_in_thread_view=False,
        selector=f"#message-list #message-{parent_message.id}",
    )

    # Off the hot path, like the rest of the notification fan-out.
    spawn_background(
        _notify_all_thread_participants, sender, parent_message, conv_id_str
    )

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
    # Group under the previous message if it's the same author within the
    # window, so a realtime-appended message matches the grouped page render.
    prev_message = previous_visible_message(new_message)
    annotate_message_grouping([new_message], prev_message=prev_message)
    new_message_html = render_template(
        "partials/message.html",
        message=new_message,
        reactions_map=reactions_map,
        attachments_map=attachments_map,
        Message=Message,
    )
    message_to_broadcast = date_separator_oob(
        new_message, prev_message, conv_id_str
    ) + message_list_append_oob(conv_id_str, new_message_html)

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
    # The account is checked when the socket opens, but a socket can outlive a
    # deactivation (disconnect_user's broadcast can be missed if Valkey blips),
    # so re-check it on every frame: no sending, subscribing or typing after.
    if User.get_active_by_id(getattr(getattr(ws, "user", None), "id", None)) is None:
        try:
            ws.close(reason=1008, message="Account deactivated")
        except Exception:  # pylint: disable=broad-exception-caught
            pass
        return
    event_type = data.get("type")
    conv_id_str = data.get("conversation_id") or getattr(ws, "channel_id", None)

    # Application-level heartbeat. Protocol pings are invisible to browser JS,
    # so a client can't tell a healthy idle socket from a half-open one (a
    # laptop that slept, a NAT that dropped the flow) without a reply it can
    # see. The web client pings every ~25s and reconnects when no pong comes.
    if event_type == "ping":
        chat_manager.send_direct(ws, {"type": "pong"})
        return

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
        # Tell the web client its socket now follows this conversation. Until
        # this lands, broadcasts for it go nowhere, so the client backfills
        # (GET .../messages/since/<id>) on the ack to close the gap between
        # the page render and the subscribe. Mobile clients don't need it.
        if not getattr(ws, "is_api_client", False):
            chat_manager.send_direct(
                ws, {"type": "subscribed", "conversation_id": conv_id_str}
            )
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

    Shared by the WebSocket path (``_process_ws_event``), the web HTTP POST
    endpoint and the mobile REST endpoint so all of them enforce identical
    access rules and produce identical broadcasts. Returns ``(status,
    message)``: the new ``Message`` on "ok", else ``None``. Status is one of:

      "ok"              — message created and broadcast
      "empty"           — nothing to send (no text, no attachments)
      "no_conversation" — conv_id_str resolves to no conversation
      "bad_request"     — malformed conversation id
      "forbidden"       — sender isn't a member / can't post here
      "bad_ref"         — a parent/quote/attachment id the sender can't use
    """
    if not chat_text and not attachment_file_ids:
        return "empty", None

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
        return "no_conversation", None

    try:
        parsed_conv = parse_conversation_id(conversation.conversation_id_str)
    except ValueError:
        return "bad_request", None

    # Membership + posting-restriction gate: any authenticated client could
    # otherwise post into any conversation by naming another conv's id.
    if not user_can_post(sender, parsed_conv):
        current_app.logger.warning(
            "Send blocked: user %s can't post to %r", sender.id, conv_id_str
        )
        return "forbidden", None

    try:
        parent_id, reply_type, quoted_message_id, file_ids = (
            chat_service.validate_message_refs(
                sender,
                conversation,
                parent_id=parent_id,
                reply_type=reply_type,
                quoted_message_id=quoted_message_id,
                attachment_file_ids=attachment_file_ids,
            )
        )
    except chat_service.MessageRefError as exc:
        current_app.logger.warning(
            "Send blocked: user %s, bad reference (%s)", sender.id, exc
        )
        return "bad_ref", None

    new_message = chat_service.handle_new_message(
        sender=sender,
        conversation=conversation,
        chat_text=chat_text,
        parent_id=parent_id,
        reply_type=reply_type,
        attachment_file_ids=file_ids,
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
    return "ok", new_message


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
    # Mobile clients don't send browser activity pings, so treat a fresh API
    # socket as "present" immediately (the heartbeat then keeps it fresh). Web
    # clients are stamped active by their own input-driven pings instead.
    if is_api:
        chat_manager.mark_active(user.id)
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
# Registered via sock.route(...)(fn) rather than as a decorator: flask_sock's
# decorator returns None, which would leave the module-level name unbound and
# the handler unreachable for direct testing (see tests/test_route_auth.py,
# which asserts these two handlers reject unauthenticated connections).
def chat(ws):
    """Handles all real-time WebSocket communication."""
    user = User.get_active_by_id(session.get("user_id"))
    if not user:
        reject_ws(ws, "Not authenticated")
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
        reject_ws(ws, "Invalid origin")
        return

    _setup_ws(ws, user)

    try:
        while True:
            _safe_handle_frame(ws, ws.receive())
    finally:
        _teardown_ws(ws, "Client")


sock.route("/ws/chat")(chat)


# --- API JSON WebSocket Handler ---
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
        reject_ws(ws, "Invalid or missing token")
        return

    user = User.get_active_by_id(user_id)
    if not user:
        reject_ws(ws, "User not found")
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


sock.route("/ws/api/v1")(api_ws)
