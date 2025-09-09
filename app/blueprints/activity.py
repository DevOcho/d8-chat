import datetime
from collections import defaultdict

from flask import Blueprint, render_template, g, make_response, url_for
from app.models import User, Message, Channel, Conversation, UserConversationStatus
from app.routes import (
    login_required,
    get_reactions_for_messages,
    get_attachments_for_messages,
)

activity_bp = Blueprint("activity", __name__)


@activity_bp.route("/chat/threads")
@login_required
def view_all_threads():
    """
    Renders a view showing all threads the current user is a participant in,
    ordered by the most recent reply.
    """
    g.user.last_threads_view_at = datetime.datetime.now()
    g.user.save()

    # Get the threads so we can show them
    user_thread_replies = Message.select().where(
        (Message.user == g.user) & (Message.reply_type == "thread")
    )
    involved_parent_ids = {reply.parent_message_id for reply in user_thread_replies}
    started_thread_parents = Message.select(Message.id).where(
        (Message.user == g.user) & (Message.last_reply_at.is_null(False))
    )
    for parent in started_thread_parents:
        involved_parent_ids.add(parent.id)
    if not involved_parent_ids:
        threads = []
    else:
        threads = list(
            Message.select()
            .where(Message.id.in_(list(involved_parent_ids)))
            .order_by(
                Message.last_reply_at.desc(nulls="LAST"), Message.created_at.desc()
            )
        )
    channel_ids_to_fetch = {
        int(t.conversation.conversation_id_str.split("_")[1])
        for t in threads
        if t.conversation.type == "channel"
    }
    channel_map = {}
    if channel_ids_to_fetch:
        channels = Channel.select().where(Channel.id.in_(list(channel_ids_to_fetch)))
        channel_map = {channel.id: channel for channel in channels}
    reactions_map = get_reactions_for_messages(threads)
    attachments_map = get_attachments_for_messages(threads)

    # 1. Main Content: The list of threads. This will be placed into the hx-target.
    threads_html = render_template(
        "partials/threads_view.html",
        threads=threads,
        reactions_map=reactions_map,
        attachments_map=attachments_map,
        Message=Message,
        channel_map=channel_map,
    )

    # 2. OOB Header: A simple header for the threads view.
    header_html = render_template("partials/threads_header.html")

    # 3. OOB Input Area: An empty div to hide the chat input.
    hide_input_html = '<div id="chat-input-container" hx-swap-oob="true"></div>'

    # 4. OOB Sidebar Link: Mark the link as read.
    read_link_html = render_template("partials/threads_link_read.html")

    # Combine all fragments into a single response.
    return make_response(threads_html + header_html + hide_input_html + read_link_html)


@activity_bp.route("/chat/unreads")
@login_required
def view_all_unreads():
    """
    Renders a view showing all unread messages for the current user,
    grouped by conversation.
    """
    unread_messages_query = (
        Message.select(Message, User, Conversation)
        .join(User)
        .switch(Message)
        .join(Conversation)
        .join(
            UserConversationStatus,
            on=(
                (UserConversationStatus.conversation == Conversation.id)
                & (UserConversationStatus.user == g.user.id)
            ),
        )
        .where(
            (Message.user != g.user)
            & (Message.created_at > UserConversationStatus.last_read_timestamp)
        )
        .order_by(Conversation.id, Message.created_at)
    )

    unread_messages = list(unread_messages_query)

    grouped_unreads = defaultdict(list)
    for msg in unread_messages:
        grouped_unreads[msg.conversation].append(msg)

    # This block handles marking conversations as read and preparing UI updates.
    oob_clear_badges_html = ""
    if grouped_unreads:
        conversations_to_update = grouped_unreads.keys()

        # Update the database in a single query
        now = datetime.datetime.now()
        UserConversationStatus.update(last_read_timestamp=now).where(
            (UserConversationStatus.user == g.user)
            & (UserConversationStatus.conversation.in_(list(conversations_to_update)))
        ).execute()

        # Prepare the OOB swaps to clear the sidebar badges
        clear_badge_fragments = []
        for conv in conversations_to_update:
            if conv.type == "channel":
                channel = Channel.get_by_id(conv.conversation_id_str.split("_")[1])
                link_text = f"# {channel.name}"
                hx_get_url = url_for("channels.get_channel_chat", channel_id=channel.id)
            else:  # DM
                user_ids = [int(uid) for uid in conv.conversation_id_str.split("_")[1:]]
                other_user_id = next(
                    (uid for uid in user_ids if uid != g.user.id), g.user.id
                )
                other_user = User.get_by_id(other_user_id)
                link_text = other_user.display_name or other_user.username
                hx_get_url = url_for("dms.get_dm_chat", other_user_id=other_user.id)

            clear_badge_fragments.append(
                render_template(
                    "partials/clear_badge.html",
                    conv_id_str=conv.conversation_id_str,
                    hx_get_url=hx_get_url,
                    link_text=link_text,
                )
            )
        oob_clear_badges_html = "".join(clear_badge_fragments)

    context_map = {}
    if grouped_unreads:
        channel_ids_to_find = set()
        dm_partner_ids_to_find = set()

        for conv in grouped_unreads.keys():
            if conv.type == "channel":
                channel_ids_to_find.add(int(conv.conversation_id_str.split("_")[1]))
            elif conv.type == "dm":
                user_ids = [int(uid) for uid in conv.conversation_id_str.split("_")[1:]]
                partner_id = next(
                    (uid for uid in user_ids if uid != g.user.id), g.user.id
                )
                dm_partner_ids_to_find.add(partner_id)

        channel_lookup = {
            c.id: f"# {c.name}"
            for c in Channel.select().where(Channel.id.in_(list(channel_ids_to_find)))
        }
        user_lookup = {
            u.id: (u.display_name or u.username)
            for u in User.select().where(User.id.in_(list(dm_partner_ids_to_find)))
        }

        for conv in grouped_unreads.keys():
            if conv.type == "channel":
                channel_id = int(conv.conversation_id_str.split("_")[1])
                context_map[conv.id] = channel_lookup.get(channel_id, "Unknown Channel")
            elif conv.type == "dm":
                user_ids = [int(uid) for uid in conv.conversation_id_str.split("_")[1:]]
                partner_id = next(
                    (uid for uid in user_ids if uid != g.user.id), g.user.id
                )
                context_map[conv.id] = user_lookup.get(partner_id, "Unknown User")

    # Fetch reactions and attachments for all the unread messages at once.
    reactions_map = get_reactions_for_messages(unread_messages)
    attachments_map = get_attachments_for_messages(unread_messages)

    # 1. Main Content: The list of unread messages.
    unreads_html = render_template(
        "partials/unreads_view.html",
        grouped_unreads=grouped_unreads,
        context_map=context_map,
        reactions_map=reactions_map,
        attachments_map=attachments_map,
        Message=Message,  # Pass the Message class for use in message.html
    )

    # 2. OOB Header: A simple header for the unreads view.
    header_html = render_template("partials/unreads_header.html")

    # 3. OOB Input Area: An empty div to hide the chat input.
    hide_input_html = '<div id="chat-input-container" hx-swap-oob="true"></div>'

    # 4. OOB Sidebar Link: Mark the link as read now that we're viewing the page.
    read_link_html = render_template("partials/unreads_link_read.html")

    # Combine all fragments into a single response.
    return make_response(
        unreads_html
        + header_html
        + hide_input_html
        + read_link_html
        + oob_clear_badges_html
    )
