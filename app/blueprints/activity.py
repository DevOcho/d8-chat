import datetime

from flask import Blueprint, render_template, g, make_response
from app.models import User, Message, Channel
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

    # --- (The logic to query for threads is correct and remains unchanged) ---
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
