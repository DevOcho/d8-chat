# app/blueprints/messages.py
import re

import markdown
from flask import Blueprint, g, json, make_response, render_template, request, url_for

from app.chat_manager import chat_manager
from app.models import (
    Channel,
    Conversation,
    Hashtag,
    Message,
    MessageHashtag,
    Reaction,
    UserConversationStatus,
    db,
)
from app.routes import (
    PAGE_SIZE,
    get_attachments_for_messages,
    get_reactions_for_messages,
    login_required,
)
from app.services import minio_service

messages_bp = Blueprint("messages", __name__)


def to_html(text):
    """Converts markdown text to HTML."""
    return markdown.markdown(text, extensions=["extra", "codehilite", "pymdownx.tilde"])


@messages_bp.route("/chat/utility/markdown-to-html", methods=["POST"])
@login_required
def markdown_to_html():
    """A utility endpoint to convert a snippet of markdown to HTML."""
    return to_html(request.form.get("text", ""))


@messages_bp.route("/chat/message/<int:message_id>", methods=["GET"])
@login_required
def get_message_view(message_id):
    """Returns the standard, read-only view of a single message."""
    message = Message.get_or_none(id=message_id)
    if not message:
        return "", 404
    reactions_map = get_reactions_for_messages([message])
    attachments_map = get_attachments_for_messages([message])
    return render_template(
        "partials/message.html",
        message=message,
        reactions_map=reactions_map,
        attachments_map=attachments_map,
        Message=Message,
    )


@messages_bp.route("/chat/message/<int:message_id>/edit", methods=["GET"])
@login_required
def get_edit_message_form(message_id):
    message = Message.get_or_none(id=message_id)
    if not message or message.user.id != g.user.id:
        return "", 403
    return render_template("partials/edit_message_form.html", message=message)


@messages_bp.route("/chat/message/<int:message_id>", methods=["PUT"])
@login_required
def update_message(message_id):
    """Handles the submission of an edited message."""
    message = Message.get_or_none(id=message_id)
    if not message or message.user.id != g.user.id:
        return "Unauthorized", 403
    is_in_thread_view = request.form.get("is_in_thread_view") == "true"
    new_content = request.form.get("content")
    if new_content:
        with db.atomic():
            MessageHashtag.delete().where(MessageHashtag.message == message).execute()
            message.content = new_content
            message.is_edited = True
            message.save()
            hashtag_names = set(re.findall(r"(?<!#)#([a-zA-Z0-9_-]+)", new_content))
            if hashtag_names:
                existing_channels = {
                    c.name
                    for c in Channel.select().where(
                        Channel.name.in_(list(hashtag_names))
                    )
                }
                valid_hashtags = hashtag_names - existing_channels
                for tag_name in valid_hashtags:
                    hashtag, _ = Hashtag.get_or_create(name=tag_name)
                    MessageHashtag.create(message=message, hashtag=hashtag)
        conv_id_str = message.conversation.conversation_id_str
        reactions_map = get_reactions_for_messages([message])
        attachments_map = get_attachments_for_messages([message])
        updated_message_html = render_template(
            "partials/message.html",
            message=message,
            reactions_map=reactions_map,
            attachments_map=attachments_map,
            Message=Message,
            is_in_thread_view=is_in_thread_view,
        )
        broadcast_html = f'<div id="message-{message.id}" hx-swap-oob="outerHTML">{updated_message_html}</div>'
        chat_manager.broadcast(conv_id_str, broadcast_html)
    return render_template(
        "partials/message.html",
        message=message,
        reactions_map=reactions_map,
        attachments_map=attachments_map,
        Message=Message,
        is_in_thread_view=is_in_thread_view,
    )


@messages_bp.route("/chat/message/<int:message_id>", methods=["DELETE"])
@login_required
def delete_message(message_id):
    """Deletes a message and its associated file attachment, if one exists."""
    message = Message.get_or_none(id=message_id)
    if not message or message.user.id != g.user.id:
        return "Unauthorized", 403
    attachments_to_delete = list(message.attachments)
    conv_id_str = message.conversation.conversation_id_str
    try:
        with db.atomic():
            message.delete_instance(recursive=True)
            for attachment in attachments_to_delete:
                try:
                    minio_service.delete_file(attachment.stored_filename)
                    attachment.delete_instance()
                except Exception as e:
                    print(
                        f"Error during attachment cleanup for message {message_id}: {e}"
                    )
    except Exception as e:
        print(f"Error deleting message {message_id}: {e}")
        return "Error deleting message", 500
    broadcast_html = f'<div id="message-{message_id}" hx-swap-oob="delete"></div>'
    chat_manager.broadcast(conv_id_str, broadcast_html)
    return "", 204


@messages_bp.route("/chat/input/default")
@login_required
def get_default_chat_input():
    """Serves the default chat input form."""
    return render_template("partials/chat_input_default.html")


@messages_bp.route("/chat/message/<int:message_id>/reply")
@login_required
def get_reply_chat_input(message_id):
    message_to_reply_to = Message.get_or_none(id=message_id)
    if not message_to_reply_to:
        return "Message not found", 404
    draft_content = request.args.get("draft", "")
    draft_content_html = to_html(draft_content) if draft_content else ""
    return render_template(
        "partials/chat_input_reply.html",
        message=message_to_reply_to,
        draft_content=draft_content,
        draft_content_html=draft_content_html,
    )


@messages_bp.route("/chat/message/<int:message_id>/load_for_thread_reply")
@login_required
def load_for_thread_reply(message_id):
    """Loads the thread chat input component configured for quoting another message."""
    try:
        message_to_reply_to = Message.get_by_id(message_id)
        if not message_to_reply_to.parent_message:
            return "Cannot reply to this message in a thread context.", 400
        return render_template(
            "partials/chat_input_thread_reply.html",
            message=message_to_reply_to,
            parent_message=message_to_reply_to.parent_message,
        )
    except Message.DoesNotExist:
        return "Message not found", 404


@messages_bp.route("/chat/thread/<int:parent_message_id>")
@login_required
def view_thread(parent_message_id):
    """Renders the thread view partial for the side panel."""
    try:
        parent_message = Message.get_by_id(parent_message_id)
    except Message.DoesNotExist:
        return "Message not found", 404
    channel = None
    if parent_message.conversation.type == "channel":
        channel = Channel.get_by_id(
            int(parent_message.conversation.conversation_id_str.split("_")[1])
        )
    thread_replies = (
        Message.select()
        .where(
            (Message.parent_message == parent_message)
            & (Message.reply_type == "thread")
        )
        .order_by(Message.created_at.asc())
    )
    all_thread_messages = [parent_message] + list(thread_replies)
    reactions_map = get_reactions_for_messages(all_thread_messages)
    attachments_map = get_attachments_for_messages(all_thread_messages)
    response = make_response(
        render_template(
            "partials/thread_view.html",
            parent_message=parent_message,
            thread_replies=thread_replies,
            reactions_map=reactions_map,
            attachments_map=attachments_map,
            channel=channel,
            Message=Message,
            is_in_thread_view=True,
        )
    )
    response.headers["HX-Trigger"] = "open-offcanvas"
    return response


@messages_bp.route("/chat/input/thread/<int:parent_message_id>")
@login_required
def get_thread_chat_input(parent_message_id):
    """Serves the dedicated chat input form for a thread view."""
    try:
        parent_message = Message.get_by_id(parent_message_id)
        return render_template(
            "partials/chat_input_thread.html", parent_message=parent_message
        )
    except Message.DoesNotExist:
        return "", 404


@messages_bp.route("/chat/message/<int:message_id>/load_for_edit")
@login_required
def load_message_for_edit(message_id):
    """Loads the main chat input component configured for editing a specific message."""
    try:
        message = Message.get_by_id(message_id)
        if message.user_id != g.user.id:
            return "Unauthorized", 403
        return render_template(
            "partials/chat_input_edit.html",
            message=message,
            message_content_html=to_html(message.content),
        )
    except Message.DoesNotExist:
        return "Message not found", 404


@messages_bp.route("/chat/message/<int:message_id>/load_for_thread_edit")
@login_required
def load_message_for_thread_edit(message_id):
    """Loads the thread chat input component configured for editing a specific message."""
    try:
        message = Message.get_by_id(message_id)
        if message.user_id != g.user.id:
            return "Unauthorized", 403
        if not message.parent_message:
            return "Cannot edit a parent message from this view.", 400
        return render_template(
            "partials/chat_input_thread_edit.html",
            message=message,
            message_content_html=to_html(message.content),
        )
    except Message.DoesNotExist:
        return "Message not found", 404


@messages_bp.route("/chat/messages/older/<string:conversation_id>")
@login_required
def get_older_messages(conversation_id):
    """Fetches a batch of older messages for a given conversation."""
    before_message_id = request.args.get("before_message_id", type=int)
    if not before_message_id:
        return "Missing 'before_message_id'", 400
    try:
        cursor_message = Message.get_by_id(before_message_id)
    except Message.DoesNotExist:
        return "Message not found", 404
    conversation = Conversation.get_or_none(conversation_id_str=conversation_id)
    if not conversation:
        return "Conversation not found", 404
    query = (
        Message.select()
        .where(
            (Message.conversation == conversation)
            & (Message.created_at < cursor_message.created_at)
        )
        .order_by(Message.created_at.desc())
        .limit(PAGE_SIZE)
    )
    messages = list(reversed(query))
    reactions_map = get_reactions_for_messages(messages)
    attachments_map = get_attachments_for_messages(messages)
    return render_template(
        "partials/message_batch.html",
        messages=messages,
        conversation_id=conversation_id,
        PAGE_SIZE=PAGE_SIZE,
        reactions_map=reactions_map,
        attachments_map=attachments_map,
        Message=Message,
    )


@messages_bp.route("/chat/message/<int:message_id>/context")
@login_required
def jump_to_message(message_id):
    """
    Finds a message, loads its conversation context with the message
    in the middle, and returns the full chat view for that context.
    """
    try:
        target_message = Message.get_by_id(message_id)
    except Message.DoesNotExist:
        return "Message not found", 404

    conversation = target_message.conversation
    is_member = (
        UserConversationStatus.select()
        .where(
            (UserConversationStatus.user == g.user)
            & (UserConversationStatus.conversation == conversation)
        )
        .exists()
    )
    if not is_member:
        return "Unauthorized", 403

    messages_before = list(
        Message.select()
        .where(
            (Message.conversation == conversation) & (Message.id < target_message.id)
        )
        .order_by(Message.created_at.desc())
        .limit(30)
    )
    messages_before.reverse()
    messages_after = list(
        Message.select()
        .where(
            (Message.conversation == conversation) & (Message.id > target_message.id)
        )
        .order_by(Message.created_at.asc())
        .limit(30)
    )
    messages = messages_before + [target_message] + messages_after
    reactions_map = get_reactions_for_messages(messages)
    attachments_map = get_attachments_for_messages(messages)
    status, created = UserConversationStatus.get_or_create(
        user=g.user, conversation=conversation
    )

    add_to_sidebar_html = ""
    clear_badge_html = ""

    if conversation.type == "channel":
        from app.models import Channel, ChannelMember

        channel = Channel.get_by_id(conversation.conversation_id_str.split("_")[1])
        members_count = (
            ChannelMember.select().where(ChannelMember.channel == channel).count()
        )
        current_user_membership = ChannelMember.get_or_none(
            user=g.user, channel=channel
        )
        header_html_content = render_template(
            "partials/channel_header.html",
            channel=channel,
            members_count=members_count,
            current_user_membership=current_user_membership,
        )
        messages_html = render_template(
            "partials/channel_messages.html",
            channel=channel,
            messages=messages,
            last_read_timestamp=status.last_read_timestamp,
            mention_message_ids=set(),
            PAGE_SIZE=PAGE_SIZE,
            reactions_map=reactions_map,
            attachments_map=attachments_map,
            Message=Message,
            conversation_id=conversation.id,
        )
        clear_badge_html = render_template(
            "partials/clear_badge.html",
            conv_id_str=conversation.conversation_id_str,
            hx_get_url=url_for("channels.get_channel_chat", channel_id=channel.id),
            link_text=f"# {channel.name}",
        )
    else:  # DM
        from app.models import User

        user_ids = [int(uid) for uid in conversation.conversation_id_str.split("_")[1:]]
        other_user_id = next((uid for uid in user_ids if uid != g.user.id), g.user.id)
        other_user = User.get_by_id(other_user_id)
        header_html_content = render_template(
            "partials/dm_header.html", other_user=other_user
        )
        messages_html = render_template(
            "partials/dm_messages.html",
            messages=messages,
            other_user=other_user,
            last_read_timestamp=status.last_read_timestamp,
            PAGE_SIZE=PAGE_SIZE,
            reactions_map=reactions_map,
            attachments_map=attachments_map,
            Message=Message,
        )

        if not created and other_user.id != g.user.id:
            clear_badge_html = render_template(
                "partials/clear_badge.html",
                conv_id_str=conversation.conversation_id_str,
                hx_get_url=url_for("dms.get_dm_chat", other_user_id=other_user.id),
                link_text=other_user.display_name or other_user.username,
            )
        elif created and other_user.id != g.user.id:
            add_to_sidebar_html = render_template(
                "partials/dm_list_item_oob.html",
                user=other_user,
                conv_id_str=conversation.conversation_id_str,
                is_online=other_user.id in chat_manager.online_users,
            )

    header_html = f'<div id="chat-header-container" hx-swap-oob="true">{header_html_content}</div>'

    full_response = messages_html + header_html + clear_badge_html + add_to_sidebar_html
    response = make_response(full_response)
    response.headers["HX-Trigger"] = json.dumps(
        {"jumpToMessage": f"#message-{message_id}"}
    )
    return response


@messages_bp.route("/chat/message/<int:message_id>/react", methods=["POST"])
@login_required
def toggle_reaction(message_id):
    """Adds or removes an emoji reaction from a message for the current user."""
    emoji = request.form.get("emoji")
    message = Message.get_or_none(id=message_id)
    if not emoji or not message:
        return "Invalid request.", 400
    existing_reaction = Reaction.get_or_none(user=g.user, message=message, emoji=emoji)
    if existing_reaction:
        existing_reaction.delete_instance()
    else:
        Reaction.create(user=g.user, message=message, emoji=emoji)
    reactions_data = get_reactions_for_messages([message])
    grouped_reactions = reactions_data.get(message.id, [])
    reactions_html_content = render_template(
        "partials/reactions.html", message=message, grouped_reactions=grouped_reactions
    )
    broadcast_html = f'<div id="reactions-container-{message.id}" hx-swap-oob="innerHTML">{reactions_html_content}</div>'
    conv_id_str = message.conversation.conversation_id_str
    chat_manager.broadcast(conv_id_str, broadcast_html)
    return broadcast_html, 200
