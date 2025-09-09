import datetime
import functools
from functools import reduce
import json
import markdown
import operator
import os
import re
import secrets
import uuid

from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    session,
    g,
    make_response,
    current_app,
    flash,
)
from flask_login import login_user, logout_user, current_user
from peewee import fn
from werkzeug.utils import secure_filename

from .models import (
    User,
    Channel,
    ChannelMember,
    Message,
    Conversation,
    Workspace,
    WorkspaceMember,
    db,
    UserConversationStatus,
    Mention,
    Reaction,
    UploadedFile,
)
from .sso import oauth  # Import the oauth object
from . import sock
from .chat_manager import chat_manager
from .services import chat_service, minio_service

# Main blueprint for general app routes
main_bp = Blueprint("main", __name__)

# Number of messages per "page" meaning how many we will load at a time if they scroll back up
PAGE_SIZE = 30

# A central map for presence status to Bootstrap CSS classes.
STATUS_CLASS_MAP = {
    "online": "bg-success",
    "away": "bg-secondary",
    "busy": "bg-warning",  # Bootstrap's yellow
}


# This function runs before every request to load the logged-in user
@main_bp.before_app_request
def load_logged_in_user():
    user_id = session.get("user_id")
    if user_id is None:
        g.user = None
    else:
        g.user = User.get_or_none(User.id == user_id)


# Decorator to require login for a route
def login_required(view):
    @functools.wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            return redirect(url_for("main.login_page"))
        return view(**kwargs)

    return wrapped_view


# --- Helpers ---
def to_html(text):
    """
    Converts markdown text to HTML, using the same extensions as the
    Jinja filter for consistency.
    """
    return markdown.markdown(text, extensions=["extra", "codehilite", "pymdownx.tilde"])


@main_bp.route("/chat/utility/markdown-to-html", methods=["POST"])
@login_required
def markdown_to_html():
    """A utility endpoint to convert a snippet of markdown to HTML."""
    markdown_text = request.form.get("text", "")
    html = to_html(markdown_text)
    return html


def get_reactions_for_messages(messages):
    """
    Efficiently fetches and groups reactions for a given list of message objects.

    Args:
        messages: A list of Peewee Message model instances.

    Returns:
        A dictionary mapping message IDs to a list of their grouped reactions.
        Example: { 123: [ {'emoji': '👍', 'count': 2, ...} ], ... }
    """
    reactions_map = {}
    if not messages:
        return reactions_map

    message_ids = [m.id for m in messages]
    # Fetch all reactions for the given messages in one query
    all_reactions_for_messages = (
        Reaction.select(Reaction, User)
        .join(User)
        .where(Reaction.message_id.in_(message_ids))
        .order_by(Reaction.created_at)
    )

    # Process into a dictionary grouped by message ID, then by emoji
    reactions_by_message = {}
    for r in all_reactions_for_messages:
        mid = r.message_id
        if mid not in reactions_by_message:
            reactions_by_message[mid] = {}
        if r.emoji not in reactions_by_message[mid]:
            reactions_by_message[mid][r.emoji] = {
                "emoji": r.emoji,
                "count": 0,
                "users": [],
                "usernames": [],
            }

        group = reactions_by_message[mid][r.emoji]
        group["count"] += 1
        group["users"].append(r.user.id)
        group["usernames"].append(r.user.username)

    # Convert the inner emoji dictionary to a list for easier template iteration
    for mid, emoji_groups in reactions_by_message.items():
        reactions_map[mid] = list(emoji_groups.values())

    return reactions_map


def get_attachments_for_messages(messages):
    """
    Efficiently fetches and groups attachment data for a given list of messages.
    Args:
        messages: A list of Peewee Message model instances.

    Returns:
        A dictionary mapping message IDs to a list of their attachments.
        Example: { 123: [ {'url': '...', 'original_filename': '...'}, ... ] }
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
        att = link.attachment

        if mid not in attachments_map:
            attachments_map[mid] = []

        attachments_map[mid].append(
            {
                "url": att.url,
                "original_filename": att.original_filename,
                "mime_type": att.mime_type,
            }
        )

    return attachments_map


def check_and_get_read_state_oob(current_user, just_read_conversation):
    """
    Checks if a user has any other unread messages after reading the current one.
    If not, returns the HTML to swap the sidebar link back to the "read" state.
    """
    # Check for any other unread messages
    has_other_unreads = (
        Message.select(fn.COUNT(Message.id))
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
            # Exclude the conversation we just marked as read
            & (Conversation.id != just_read_conversation.id)
        )
        .exists()
    )

    # If there are no other unreads, return the HTML to mark the link as read.
    if not has_other_unreads:
        return render_template("partials/unreads_link_read.html")

    return ""


def get_attachments_for_messages(messages):
    """
    Efficiently fetches and groups attachment data for a given list of messages.

    Args:
        messages: A list of Peewee Message model instances.

    Returns:
        A dictionary mapping message IDs to a list of their attachments.
        Example: { 123: [ {'url': '...', 'filename': '...'}, ... ] }
    """
    attachments_map = {}
    if not messages:
        return attachments_map

    # We need these models for the query
    from .models import MessageAttachment, UploadedFile

    message_ids = [m.id for m in messages]

    # [THE FIX] Query the MessageAttachment table directly and join the file data to it.
    # This is a more direct and reliable way to get the data.
    all_links = (
        MessageAttachment.select(MessageAttachment, UploadedFile)
        .join(UploadedFile)
        .where(MessageAttachment.message_id.in_(message_ids))
    )

    for link in all_links:
        # `link.message_id` is the ID of the message this attachment belongs to.
        mid = link.message_id
        # `link.attachment` is the full UploadedFile object.
        att = link.attachment

        if mid not in attachments_map:
            attachments_map[mid] = []

        attachments_map[mid].append(
            {
                "url": att.url,
                "filename": att.original_filename,
                "mime_type": att.mime_type,
            }
        )

    return attachments_map


# --- Routes ---
@main_bp.route("/")
def index():
    return render_template("index.html")


@main_bp.route("/login")
def login_page():
    return render_template("login.html")


@main_bp.route("/login", methods=["POST"])
def login():
    """Handles username/password login form submission."""
    username = request.form.get("username")
    password = request.form.get("password")

    # Find the user by username or email
    user = User.get_or_none((User.username == username) | (User.email == username))

    # Check if user exists and password is correct
    if user and user.check_password(password):
        # Use flask-login to manage the session
        login_user(user)
        # We still set this for compatibility with our existing g.user loader
        session["user_id"] = user.id
        return redirect(url_for("main.chat_interface"))

    # If login fails, redirect back to the main page with an error
    return redirect(url_for("main.index", error="Invalid username or password."))


@main_bp.route("/sso-login")
def sso_login():
    """Redirects to the SSO provider for login."""

    redirect_uri = url_for("main.authorize", _external=True)

    # Generate a cryptographically secure nonce
    nonce = secrets.token_urlsafe(16)
    # Store the nonce in the session for later verification
    session["nonce"] = nonce

    return oauth.authentik.authorize_redirect(redirect_uri, nonce=nonce)


@main_bp.route("/auth")
def authorize():
    """The callback route for the SSO provider."""
    # The actual logic is in app/sso.py, but we need the route here
    from .sso import handle_auth_callback

    return handle_auth_callback()


@main_bp.route("/logout")
def logout():
    """Logs the user out by clearing the session."""
    logout_user()
    session.clear()
    return redirect(url_for("main.index"))


# A simple profile page to show after login
@main_bp.route("/profile")
@login_required
def profile():
    """Renders the profile details partial for the offcanvas panel."""

    html = render_template(
        "partials/profile_details.html", user=g.user, theme=g.user.theme
    )
    response = make_response(html)
    response.headers["HX-Trigger"] = "open-offcanvas"
    return response


# --- CHAT INTERFACE ROUTES ---
@main_bp.route("/chat")
@login_required
def chat_interface():
    """Renders the main chat UI."""

    # 1. Fetch all channels the user is a member of.
    user_channels = (
        Channel.select()
        .join(ChannelMember)
        .where(ChannelMember.user == g.user)
        .order_by(Channel.name)
    )

    # 2. Get all relevant conversation records in a single batch.
    # This includes all DMs this user is part of, plus all conversations for their channels.
    dm_convs_query = (
        Conversation.select()
        .join(UserConversationStatus)
        .where((UserConversationStatus.user == g.user) & (Conversation.type == "dm"))
    )

    channel_conv_ids_to_find = [f"channel_{c.id}" for c in user_channels]
    channel_convs_query = Conversation.select().where(
        Conversation.conversation_id_str.in_(channel_conv_ids_to_find)
    )

    all_conversations = list(dm_convs_query | channel_convs_query)

    # 3. Process conversations: create a lookup map and find DM partners.
    conv_map = {conv.conversation_id_str: conv for conv in all_conversations}
    dm_partner_ids = set()

    for conv in all_conversations:
        if conv.type == "dm":
            user_ids = [int(uid) for uid in conv.conversation_id_str.split("_")[1:]]
            partner_id = next((uid for uid in user_ids if uid != g.user.id), None)
            if partner_id:
                dm_partner_ids.add(partner_id)

    # Attach conversation objects to the channel models for easy access later.
    for channel in user_channels:
        channel.conversation = conv_map.get(f"channel_{channel.id}")

    # 4. Fetch the User objects for the DM sidebar.
    direct_message_users = User.select().where(User.id.in_(list(dm_partner_ids)))

    # 5. Calculate Unread Information for all relevant conversations.
    #    For channels, we differentiate between any unread messages (for bolding)
    #    and unread mentions (for a red badge). For DMs, any unread message
    #    gets a badge.
    unread_info = {}
    if all_conversations:
        user_statuses = (
            UserConversationStatus.select()
            .where(UserConversationStatus.user == g.user)
            .join(Conversation)
            .where(Conversation.id.in_([c.id for c in all_conversations]))
        )

        last_read_map = {
            status.conversation.id: status.last_read_timestamp
            for status in user_statuses
        }

        for conv in all_conversations:
            last_read_time = last_read_map.get(conv.id, datetime.datetime.min)
            mention_count = 0
            has_unread = False

            if conv.type == "channel":
                # For channels, the count is only for mentions.
                mention_count = (
                    Mention.select()
                    .join(Message)
                    .where(
                        (Mention.user == g.user)
                        & (Message.conversation == conv)
                        & (Message.created_at > last_read_time)
                    )
                    .count()
                )
                # And we separately check for any unread messages to make the link bold.
                has_unread = (
                    mention_count > 0
                    or Message.select()
                    .where(
                        (Message.conversation == conv)
                        & (Message.created_at > last_read_time)
                        & (Message.user != g.user)
                    )
                    .exists()
                )
            else:  # It's a DM
                # For DMs, the count is for all unread messages.
                mention_count = (
                    Message.select()
                    .where(
                        (Message.conversation_id == conv.id)
                        & (Message.created_at > last_read_time)
                        & (Message.user != g.user)
                    )
                    .count()
                )
                has_unread = mention_count > 0

            unread_info[conv.conversation_id_str] = {
                "mentions": mention_count,
                "has_unread": has_unread,
            }

    # Check if any of the conversations have unread messages.
    has_unreads = any(info["has_unread"] for info in unread_info.values())

    # 6. Check for unread threads to determine if the sidebar link should be bold.
    last_view_time = g.user.last_threads_view_at or datetime.datetime.min

    # Find all parent message IDs the user is involved in (started or replied to)
    user_thread_replies = Message.select().where(
        (Message.user == g.user) & (Message.reply_type == "thread")
    )
    involved_parent_ids = {reply.parent_message_id for reply in user_thread_replies}
    started_thread_parents = Message.select(Message.id).where(
        (Message.user == g.user) & (Message.last_reply_at.is_null(False))
    )
    for parent in started_thread_parents:
        involved_parent_ids.add(parent.id)

    has_unread_threads = False
    if involved_parent_ids:
        has_unread_threads = (
            Message.select(fn.COUNT(Message.id))
            .where(
                (Message.id.in_(list(involved_parent_ids)))
                & (Message.last_reply_at > last_view_time)
            )
            .exists()
        )

    return render_template(
        "chat.html",
        channels=user_channels,
        direct_message_users=direct_message_users,
        online_users=chat_manager.online_users,
        unread_info=unread_info,
        has_unreads=has_unreads,
        has_unread_threads=has_unread_threads,
        theme=g.user.theme,
    )


# --- MESSAGE EDIT AND DELETE ROUTES ---
@main_bp.route("/chat/message/<int:message_id>", methods=["GET"])
@login_required
def get_message_view(message_id):
    """Returns the standard, read-only view of a single message."""
    message = Message.get_or_none(id=message_id)

    if not message:
        return "", 404

    # This is used by the "Cancel" button on the edit form.
    reactions_map = get_reactions_for_messages([message])
    attachments_map = get_attachments_for_messages([message])
    return render_template(
        "partials/message.html",
        message=message,
        reactions_map=reactions_map,
        attachments_map=attachments_map,
        Message=Message,
    )


@main_bp.route("/chat/message/<int:message_id>/edit", methods=["GET"])
@login_required
def get_edit_message_form(message_id):
    message = Message.get_or_none(id=message_id)
    if not message or message.user.id != g.user.id:
        return "", 403
    return render_template("partials/edit_message_form.html", message=message)


@main_bp.route("/chat/message/<int:message_id>", methods=["PUT"])
@login_required
def update_message(message_id):
    """
    Handles the submission of an edited message.
    """
    message = Message.get_or_none(id=message_id)
    if not message or message.user.id != g.user.id:
        return "Unauthorized", 403

    # Check if the edit is happening within the thread view context
    is_in_thread_view = request.form.get("is_in_thread_view") == "true"

    new_content = request.form.get("content")
    if new_content:
        # Update the message in the database
        message.content = new_content
        message.is_edited = True
        message.save()

        # Get the conversation ID string for the broadcast
        conv_id_str = message.conversation.conversation_id_str

        # Render the updated message partial
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

        # Construct the OOB swap HTML for the broadcast.
        broadcast_html = f'<div id="message-{message.id}" hx-swap-oob="outerHTML">{updated_message_html}</div>'

        # Broadcast the HTML fragment to all subscribers of the conversation
        chat_manager.broadcast(conv_id_str, broadcast_html)

    # The original hx-put request also needs a response. Return the updated partial.
    return render_template(
        "partials/message.html",
        message=message,
        reactions_map=reactions_map,
        attachments_map=attachments_map,
        Message=Message,
        is_in_thread_view=is_in_thread_view,
    )


@main_bp.route("/chat/message/<int:message_id>", methods=["DELETE"])
@login_required
def delete_message(message_id):
    """
    Deletes a message and its associated file attachment, if one exists.
    """
    message = Message.get_or_none(id=message_id)
    if not message or message.user.id != g.user.id:
        return "Unauthorized", 403

    # Use the correct 'attachments' property and prepare to loop
    attachments_to_delete = list(message.attachments)
    conv_id_str = message.conversation.conversation_id_str

    try:
        # Use a database transaction to ensure all deletes succeed or fail together.
        with db.atomic():
            # Delete the message from the database
            message.delete_instance(
                recursive=True
            )  # recursive will delete related MessageAttachment links

            # If there were attachments, delete them from Minio and our records.
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

    # Construct and broadcast the UI update
    broadcast_html = f'<div id="message-{message_id}" hx-swap-oob="delete"></div>'
    chat_manager.broadcast(conv_id_str, broadcast_html)

    return "", 204


@main_bp.route("/chat/input/default")
@login_required
def get_default_chat_input():
    """Serves the default chat input form."""
    return render_template("partials/chat_input_default.html")


@main_bp.route("/chat/message/<int:message_id>/reply")
@login_required
def get_reply_chat_input(message_id):
    message_to_reply_to = Message.get_or_none(id=message_id)
    if not message_to_reply_to:
        return "Message not found", 404

    # Check for draft content from the client and keep it
    draft_content = request.args.get("draft", "")
    draft_content_html = to_html(draft_content) if draft_content else ""

    return render_template(
        "partials/chat_input_reply.html",
        message=message_to_reply_to,
        draft_content=draft_content,
        draft_content_html=draft_content_html,
    )


@main_bp.route("/chat/message/<int:message_id>/load_for_thread_reply")
@login_required
def load_for_thread_reply(message_id):
    """
    Loads the thread chat input component configured for quoting another message
    within the thread.
    """
    try:
        message_to_reply_to = Message.get_by_id(message_id)

        # A reply within a thread must itself be a child of a parent message.
        if not message_to_reply_to.parent_message:
            return "Cannot reply to this message in a thread context.", 400

        parent_message = message_to_reply_to.parent_message

        return render_template(
            "partials/chat_input_thread_reply.html",
            message=message_to_reply_to,
            parent_message=parent_message,
        )
    except Message.DoesNotExist:
        return "Message not found", 404


@main_bp.route("/chat/thread/<int:parent_message_id>")
@login_required
def view_thread(parent_message_id):
    """Renders the thread view partial for the side panel."""
    try:
        parent_message = Message.get_by_id(parent_message_id)
    except Message.DoesNotExist:
        return "Message not found", 404

    channel = None
    if parent_message.conversation.type == "channel":
        channel_id = int(parent_message.conversation.conversation_id_str.split("_")[1])
        channel = Channel.get_by_id(channel_id)

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
            is_in_thread_view=True,  # This flag tells the templates they are in the side panel
        )
    )
    response.headers["HX-Trigger"] = "open-offcanvas"
    return response


@main_bp.route("/chat/input/thread/<int:parent_message_id>")
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


# --- PROFILE EDITING ROUTES ---
@main_bp.route("/profile/address/view", methods=["GET"])
@login_required
def get_address_display():
    """Returns the read-only address display partial."""
    return render_template("partials/address_display.html", user=g.user)


@main_bp.route("/profile/avatar", methods=["POST"])
@login_required
def upload_avatar():
    if "avatar" not in request.files:
        return "No file part", 400
    file = request.files["avatar"]
    if file.filename == "":
        return "No selected file", 400

    allowed_extensions = {"png", "jpg", "jpeg", "gif"}
    if (
        "." not in file.filename
        or file.filename.rsplit(".", 1)[1].lower() not in allowed_extensions
    ):
        return "File type not allowed", 400

    old_avatar_file = g.user.avatar
    original_filename = secure_filename(file.filename)
    file_ext = original_filename.rsplit(".", 1)[1].lower()
    stored_filename = f"{uuid.uuid4()}.{file_ext}"

    temp_dir = os.path.join(current_app.instance_path, "temp_uploads")
    os.makedirs(temp_dir, exist_ok=True)
    temp_path = os.path.join(temp_dir, stored_filename)

    try:
        file.save(temp_path)
        file_size = os.path.getsize(temp_path)

        success = minio_service.upload_file(
            object_name=stored_filename, file_path=temp_path, content_type=file.mimetype
        )

        if success:
            new_file = UploadedFile.create(
                uploader=g.user,
                original_filename=original_filename,
                stored_filename=stored_filename,
                mime_type=file.mimetype,
                file_size_bytes=file_size,
            )
            g.user.avatar = new_file
            g.user.save()

            # If an old avatar existed, delete it now.
            if old_avatar_file:
                try:
                    # Delete from Minio
                    minio_service.delete_file(old_avatar_file.stored_filename)
                    # Delete from our database
                    old_avatar_file.delete_instance()
                except Exception as e:
                    # If cleanup fails, log it but don't fail the whole request.
                    # The user's avatar has been successfully updated.
                    print(f"Error during old avatar cleanup: {e}")

            # Broadcast a JSON event for EVERYONE ELSE to update their views.
            avatar_update_payload = {
                "type": "avatar_update",
                "user_id": g.user.id,
                "avatar_url": g.user.avatar_url,
            }
            chat_manager.broadcast(
                None,
                avatar_update_payload,
                sender_ws=chat_manager.all_clients.get(g.user.id),
                is_event=True,
            )

            # Prepare a multi-part HTTP response for the UPLOADER.
            #  - The main response updates the profile header (the hx-target).
            #  - The OOB swap updates the uploader's own sidebar button.
            profile_header_html = render_template(
                "partials/profile_header.html", user=g.user
            )
            sidebar_button_html = render_template(
                "partials/_sidebar_profile_button.html"
            )
            sidebar_oob_swap = f'<div hx-swap-oob="outerHTML:#sidebar-profile-button">{sidebar_button_html}</div>'

            return make_response(profile_header_html + sidebar_oob_swap)

    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

    # Fallback in case of upload failure
    return render_template("partials/profile_header.html", user=g.user)


@main_bp.route("/profile/address/edit", methods=["GET"])
@login_required
def get_address_form():
    """Returns the address editing form partial."""
    return render_template("partials/address_form.html", user=g.user)


@main_bp.route("/profile/address", methods=["PUT"])
@login_required
def update_address():
    """Processes the address form submission."""
    user = g.user
    user.country = request.form.get("country")
    user.city = request.form.get("city")
    user.timezone = request.form.get("timezone")
    user.save()

    # This is the primary, "in-band" response for the hx-target.
    display_html = render_template("partials/address_display.html", user=user)

    # Explicitly create the OOB swap for the header in the backend.
    header_html_content = render_template("partials/profile_header.html", user=user)
    header_oob_swap = f'<div id="profile-header-card" hx-swap-oob="outerHTML">{header_html_content}</div>'

    # Combine them.
    full_response = display_html + header_oob_swap

    return make_response(full_response)


@main_bp.route("/chat/message/<int:message_id>/react", methods=["POST"])
@login_required
def toggle_reaction(message_id):
    """Adds or removes an emoji reaction from a message for the current user."""
    emoji = request.form.get("emoji")
    message = Message.get_or_none(id=message_id)

    if not emoji or not message:
        return "Invalid request.", 400

    # Check if the reaction already exists for this user/message/emoji
    existing_reaction = Reaction.get_or_none(user=g.user, message=message, emoji=emoji)

    if existing_reaction:
        # If it exists, delete it (this is the "toggle off" action).
        existing_reaction.delete_instance()
    else:
        # If it doesn't exist, create it.
        Reaction.create(user=g.user, message=message, emoji=emoji)

    # 1. Call the helper function to get the dictionary of reactions.
    reactions_data = get_reactions_for_messages([message])
    # 2. Correctly extract the LIST of reactions for this specific message.
    grouped_reactions = reactions_data.get(message.id, [])

    # Render the reactions partial with the new, correct data
    reactions_html_content = render_template(
        "partials/reactions.html", message=message, grouped_reactions=grouped_reactions
    )

    # Wrap the content in the container div for the OOB broadcast
    broadcast_html = f'<div id="reactions-container-{message.id}" hx-swap-oob="innerHTML">{reactions_html_content}</div>'

    # Broadcast the updated HTML to everyone else in the conversation
    conv_id_str = message.conversation.conversation_id_str

    # We exclude the sender because we're about to send them the response directly.
    chat_manager.broadcast(
        conv_id_str, broadcast_html, sender_ws=chat_manager.all_clients.get(g.user.id)
    )

    # Return the HTML directly to the user who clicked. HTMX will swap it instantly.
    return broadcast_html, 200


@main_bp.route("/profile/status", methods=["PUT"])
@login_required
def update_presence_status():
    """Updates the user's presence status and broadcasts the change."""
    new_status = request.form.get("status")
    if new_status and new_status in STATUS_CLASS_MAP:
        user = g.user
        user.presence_status = new_status
        user.save()

        # Use consistent presence classes for all broadcasts
        presence_class_map = {
            "online": "presence-online",
            "away": "presence-away",
            "busy": "presence-busy",
        }
        presence_class = presence_class_map.get(new_status, "presence-away")

        # Broadcast the DM list update
        dm_list_presence_html = f'<span id="status-dot-{user.id}" class="presence-indicator {presence_class}" hx-swap-oob="true"></span>'
        chat_manager.broadcast_to_all(dm_list_presence_html)

        # Broadcast the sidebar button update
        sidebar_presence_html = f'<span id="sidebar-presence-indicator-{user.id}" class="presence-indicator {presence_class}" hx-swap-oob="true"></span>'
        chat_manager.broadcast_to_all(sidebar_presence_html)

        # Prepare the multi-part HTTP response for the user who made the change.
        profile_header_html = render_template(
            "partials/profile_header.html", user=g.user
        )
        sidebar_button_html = render_template("partials/_sidebar_profile_button.html")
        sidebar_oob_swap = f'<div hx-swap-oob="outerHTML:#sidebar-profile-button">{sidebar_button_html}</div>'

        return make_response(profile_header_html + sidebar_oob_swap)

    return "Invalid status", 400


@main_bp.route("/profile/theme", methods=["PUT"])
@login_required
def update_theme():
    """Updates the user's theme preference."""
    new_theme = request.form.get("theme")
    if new_theme in ["light", "dark", "system"]:
        user = g.user
        user.theme = new_theme
        user.save()
        # Instruct the browser to do a full reload to apply the new theme
        response = make_response("")
        response.headers["HX-Refresh"] = "true"
        return response
    return "Invalid theme", 400


@main_bp.route("/profile/notification_sound", methods=["PUT"])
@login_required
def update_notification_sound():
    """Updates the user's notification sound preference."""
    new_sound = request.form.get("sound")
    # A list of the sounds we know are available.
    allowed_sounds = ["d8-notification.mp3", "slack-notification.mp3"]

    if new_sound and new_sound in allowed_sounds:
        user = g.user
        user.notification_sound = new_sound
        user.save()

        # We'll trigger a custom event that our JavaScript can listen for
        # to update the sound without a page reload.
        response = make_response("")
        response.headers["HX-Trigger"] = json.dumps(
            {"update-sound-preference": new_sound}
        )
        return response

    return "Invalid sound choice", 400


@main_bp.route("/chat/user/preference/wysiwyg", methods=["PUT"])
@login_required
def set_wysiwyg_preference():
    """Updates the user's preference for the WYSIWYG editor."""
    # The value comes from our JS, default to 'false' if not provided
    enabled_str = request.form.get("wysiwyg_enabled", "false")
    enabled = enabled_str.lower() == "true"

    # Update the user record only if the value has changed
    if g.user.wysiwyg_enabled != enabled:
        user = User.get_by_id(g.user.id)
        user.wysiwyg_enabled = enabled
        user.save()
        # g.user is a snapshot from the start of the request,
        # so we update it too for the current request context.
        g.user.wysiwyg_enabled = enabled

    # Return a 204 No Content response, as HTMX doesn't need to swap anything
    return "", 204


@main_bp.route("/chat/message/<int:message_id>/load_for_edit")
@login_required
def load_message_for_edit(message_id):
    """
    Loads the main chat input component configured for editing a specific message.
    """
    try:
        message = Message.get_by_id(message_id)
        if message.user_id != g.user.id:
            return "Unauthorized", 403

        # Convert markdown to HTML for the WYSIWYG view
        message_content_html = to_html(message.content)

        return render_template(
            "partials/chat_input_edit.html",
            message=message,
            message_content_html=message_content_html,
        )
    except Message.DoesNotExist:
        return "Message not found", 404
    return render_template("partials/chat_input_edit.html", message=message)


@main_bp.route("/chat/message/<int:message_id>/load_for_thread_edit")
@login_required
def load_message_for_thread_edit(message_id):
    """
    Loads the thread chat input component configured for editing a specific message.
    """
    try:
        message = Message.get_by_id(message_id)
        if message.user_id != g.user.id:
            return "Unauthorized", 403

        # We need the parent message context to correctly ID the container
        if not message.parent_message:
            return "Cannot edit a parent message from this view.", 400

        # Convert markdown to HTML for the WYSIWYG view
        message_content_html = to_html(message.content)

        return render_template(
            "partials/chat_input_thread_edit.html",
            message=message,
            message_content_html=message_content_html,
        )
    except Message.DoesNotExist:
        return "Message not found", 404


@main_bp.route("/chat/messages/older/<string:conversation_id>")
@login_required
def get_older_messages(conversation_id):
    """Fetches a batch of older messages for a given conversation."""
    before_message_id = request.args.get("before_message_id", type=int)
    if not before_message_id:
        return "Missing 'before_message_id'", 400

    try:
        # Get the timestamp of the message we are paginating from
        cursor_message = Message.get_by_id(before_message_id)
        cursor_timestamp = cursor_message.created_at
    except Message.DoesNotExist:
        return "Message not found", 404

    # Find the corresponding conversation record
    conversation = Conversation.get_or_none(conversation_id_str=conversation_id)
    if not conversation:
        return "Conversation not found", 404

    # Base query
    query = (
        Message.select()
        .where(
            (Message.conversation == conversation)
            & (Message.created_at < cursor_timestamp)
        )
        .order_by(Message.created_at.desc())
        .limit(PAGE_SIZE)
    )

    # Fetch and reverse the messages for correct chronological display
    messages = list(reversed(query))

    # The new partial handles rendering the messages and the next trigger
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


@main_bp.route("/chat/message/<int:message_id>/context")
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
        channel = Channel.get_by_id(conversation.conversation_id_str.split("_")[1])
        members_count = (
            ChannelMember.select().where(ChannelMember.channel == channel).count()
        )
        header_html_content = render_template(
            "partials/channel_header.html", channel=channel, members_count=members_count
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
        )
        clear_badge_html = render_template(
            "partials/clear_badge.html",
            conv_id_str=conversation.conversation_id_str,
            hx_get_url=url_for("channels.get_channel_chat", channel_id=channel.id),
            link_text=f"# {channel.name}",
        )
    else:  # DM
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


# --- WebSocket Handler ---
@sock.route("/ws/chat")
def chat(ws):
    print("INFO: WebSocket client connected.")
    user = session.get("user_id") and User.get_or_none(id=session.get("user_id"))
    if not user:
        print("ERROR: Unauthenticated user tried to connect. Closing.")
        ws.close(reason=1008, message="Not authenticated")
        return
    ws.user = user

    chat_manager.set_online(user.id, ws)

    presence_class_map = {
        "online": "presence-online",
        "away": "presence-away",
        "busy": "presence-busy",
    }
    presence_class = presence_class_map.get(user.presence_status, "presence-away")

    # Update for the DM list
    dm_list_presence_html = f'<span id="status-dot-{user.id}" class="presence-indicator {presence_class}" hx-swap-oob="true"></span>'
    chat_manager.broadcast_to_all(dm_list_presence_html)

    # Update for the main sidebar profile button
    sidebar_presence_html = f'<span id="sidebar-presence-indicator-{user.id}" class="presence-indicator {presence_class}" hx-swap-oob="true"></span>'
    chat_manager.broadcast_to_all(sidebar_presence_html)

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
                indicator_html = f'<div id="typing-indicator" hx-swap-oob="true">{f"<p>{ws.user.username} is typing...</p>" if is_typing else ""}</div>'
                chat_manager.broadcast(conv_id_str, indicator_html, sender_ws=ws)
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

            # --- NEW BROADCAST LOGIC ---
            if new_message.reply_type == "thread":
                # This is a threaded reply.
                broadcast_html = ""

                # 1. The new message to append to the thread panel for those viewing it.
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

                # 2. The updated parent message for the main channel view (shows the new reply count).
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

                # 3. The "unread" indicator for the sidebar "Threads" link.
                all_participant_ids = {parent_message.user_id}
                replies = Message.select(Message.user_id).where(
                    Message.parent_message == parent_message
                )
                for reply in replies:
                    all_participant_ids.add(reply.user_id)

                unread_link_html = render_template("partials/threads_link_unread.html")
                for user_id in all_participant_ids:
                    if user_id != ws.user.id and user_id in chat_manager.all_clients:
                        chat_manager.send_to_user(user_id, unread_link_html)

                all_participant_ids = {parent_message.user_id}
                replies = Message.select(Message.user_id).where(
                    Message.parent_message == parent_message
                )
                for reply in replies:
                    all_participant_ids.add(reply.user_id)

                unread_link_html = render_template("partials/threads_link_unread.html")
                now = datetime.datetime.now()

                for user_id in all_participant_ids:
                    # Don't notify the sender or users who are not online
                    if user_id == ws.user.id or user_id not in chat_manager.all_clients:
                        continue

                    # Send the "unread" indicator to the sidebar
                    chat_manager.send_to_user(user_id, unread_link_html)

                    # Now, check if we should also send a sound notification
                    try:
                        # We need the user's status for the PARENT conversation
                        # to check their notification cooldown.
                        status, _ = UserConversationStatus.get_or_create(
                            user_id=user_id, conversation=parent_message.conversation
                        )

                        should_notify = status.last_notified_timestamp is None or (
                            now - status.last_notified_timestamp
                        ) > datetime.timedelta(seconds=10)

                        if should_notify:
                            sound_payload = {"type": "sound"}
                            chat_manager.send_to_user(user_id, sound_payload)
                            status.last_notified_timestamp = now
                            status.save()

                    except Exception as e:
                        print(
                            f"Error sending thread notification to user {user_id}: {e}"
                        )

                # Broadcast to all clients in the conversation and send back to the sender.
                chat_manager.broadcast(conv_id_str, broadcast_html, sender_ws=ws)
                ws.send(broadcast_html)

            else:
                # This is a regular message or a quoted reply, which appears in the main feed.
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

                # Broadcast to others
                chat_manager.broadcast(conv_id_str, message_to_broadcast, sender_ws=ws)

                # Send back to the sender, potentially with an input reset for quoted replies.
                message_for_sender = message_to_broadcast
                if new_message.reply_type == "quote":
                    input_html = render_template("partials/chat_input_default.html")
                    message_for_sender += f'<div id="chat-input-container" hx-swap-oob="outerHTML">{input_html}</div>'
                ws.send(message_for_sender)

            # --- Notification logic for users NOT viewing the channel remains the same ---
            # (The existing notification logic from line 976 onwards is fine)
            if conversation.type == "channel":
                channel_id = conversation.conversation_id_str.split("_")[1]
                channel = Channel.get_by_id(channel_id)
                members = (
                    User.select()
                    .join(ChannelMember)
                    .where(ChannelMember.channel == channel)
                )
            else:
                user_ids = [int(uid) for uid in conv_id_str.split("_")[1:]]
                members = User.select().where(User.id.in_(user_ids))

            for member in members:
                if member.id == ws.user.id or member.id not in chat_manager.all_clients:
                    continue

                member_ws = chat_manager.all_clients[member.id]

                status, _ = UserConversationStatus.get_or_create(
                    user=member, conversation=conversation
                )

                notification_html = None
                if conversation.type == "channel":
                    channel_model = Channel.get_by_id(
                        conversation.conversation_id_str.split("_")[1]
                    )
                    link_text = f"# {channel_model.name}"
                    hx_get_url = url_for(
                        "channels.get_channel_chat", channel_id=channel_model.id
                    )
                    new_mention_count = (
                        Mention.select()
                        .join(Message)
                        .where(
                            (Message.created_at > status.last_read_timestamp)
                            & (Mention.user == member)
                            & (Message.conversation == conversation)
                        )
                        .count()
                    )
                    if new_mention_count > 0:
                        notification_html = render_template(
                            "partials/unread_badge.html",
                            conv_id_str=conv_id_str,
                            count=new_mention_count,
                            link_text=link_text,
                            hx_get_url=hx_get_url,
                        )
                    elif (
                        Message.select()
                        .where(
                            (Message.conversation == conversation)
                            & (Message.created_at > status.last_read_timestamp)
                        )
                        .exists()
                    ):
                        notification_html = render_template(
                            "partials/bold_link.html",
                            conv_id_str=conv_id_str,
                            link_text=link_text,
                            hx_get_url=hx_get_url,
                        )
                else:  # DM
                    link_text = ws.user.display_name or ws.user.username
                    hx_get_url = url_for("dms.get_dm_chat", other_user_id=ws.user.id)
                    new_count = (
                        Message.select()
                        .where(
                            (Message.conversation == conversation)
                            & (Message.created_at > status.last_read_timestamp)
                            & (Message.user != member)
                        )
                        .count()
                    )
                    if new_count > 0:
                        notification_html = render_template(
                            "partials/unread_badge.html",
                            conv_id_str=conv_id_str,
                            count=new_count,
                            link_text=link_text,
                            hx_get_url=hx_get_url,
                        )

                if notification_html:
                    unread_link_html = render_template(
                        "partials/unreads_link_unread.html"
                    )
                    member_ws.send(notification_html)
                    member_ws.send(unread_link_html)

                now = datetime.datetime.now()
                is_a_mention = (
                    Mention.select()
                    .where((Mention.message == new_message) & (Mention.user == member))
                    .exists()
                )

                # Rule 1: Always notify with sound and a desktop notification for any mention
                if is_a_mention:
                    sound_payload = {"type": "sound"}
                    chat_manager.send_to_user(member.id, sound_payload)

                    notification_payload = {
                        "type": "notification",
                        "title": f"New mention from {new_message.user.display_name or new_message.user.username}",
                        "body": new_message.content,
                        "icon": url_for(
                            "static", filename="favicon.ico", _external=True
                        ),
                        "tag": conv_id_str,
                    }
                    chat_manager.send_to_user(member.id, notification_payload)
                    status.last_notified_timestamp = now
                    status.save()

                # Rule 2: If it's not a mention, ONLY notify with sound if it's a DM (and respect the cooldown)
                elif conversation.type == "dm":
                    should_notify = status.last_notified_timestamp is None or (
                        now - status.last_notified_timestamp
                    ) > datetime.timedelta(seconds=10)
                    if should_notify:
                        sound_payload = {"type": "sound"}
                        chat_manager.send_to_user(member.id, sound_payload)
                        status.last_notified_timestamp = now
                        status.save()

    finally:
        if hasattr(ws, "user") and ws.user:
            user_id = ws.user.id
            chat_manager.set_offline(user_id)

            # Broadcast consistent updates for BOTH indicators on disconnect
            dm_list_presence_html = f'<span id="status-dot-{user_id}" class="presence-indicator presence-away" hx-swap-oob="true"></span>'
            chat_manager.broadcast_to_all(dm_list_presence_html)

            sidebar_presence_html = f'<span id="sidebar-presence-indicator-{user_id}" class="presence-indicator presence-away" hx-swap-oob="true"></span>'
            chat_manager.broadcast_to_all(sidebar_presence_html)

            chat_manager.unsubscribe(ws)
            print(f"INFO: Client connection closed for '{ws.user.username}'.")
