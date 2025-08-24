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
)
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

# Admin blueprint for admin-specific routes
admin_bp = Blueprint("admin", __name__)

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


# --- Routes ---
@main_bp.route("/")
def index():
    return render_template("index.html")


@main_bp.route("/login")
def login_page():
    return render_template("login.html")


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
    session.clear()
    return redirect(url_for("main.index"))


# A simple profile page to show after login
@main_bp.route("/profile")
@login_required
def profile():
    """Renders the profile details partial for the offcanvas panel."""
    return render_template(
        "partials/profile_details.html", user=g.user, theme=g.user.theme
    )


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

    return render_template(
        "chat.html",
        channels=user_channels,
        direct_message_users=direct_message_users,
        online_users=chat_manager.online_users,
        unread_info=unread_info,
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
    return render_template("partials/message.html", message=message)


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

    new_content = request.form.get("content")
    if new_content:
        # Update the message in the database
        message.content = new_content
        message.is_edited = True
        message.save()

        # Get the conversation ID string for the broadcast
        conv_id_str = message.conversation.conversation_id_str

        # Render the updated message partial
        updated_message_html = render_template("partials/message.html", message=message)

        # Construct the OOB swap HTML for the broadcast. This tells all
        # clients to replace the message's outer HTML with the updated version.
        broadcast_html = f'<div id="message-{message.id}" hx-swap-oob="outerHTML">{updated_message_html}</div>'

        # Broadcast the HTML fragment to all subscribers of the conversation
        chat_manager.broadcast(conv_id_str, broadcast_html)

    # The original hx-put request also needs a response. Return the updated partial.
    return render_template("partials/message.html", message=message)


@main_bp.route("/chat/message/<int:message_id>", methods=["DELETE"])
@login_required
def delete_message(message_id):
    """
    Deletes a message and its associated file attachment, if one exists.
    """
    message = Message.get_or_none(id=message_id)
    if not message or message.user.id != g.user.id:
        return "Unauthorized", 403

    # [THE FIX] Check for an attachment *before* deleting the message.
    attachment_to_delete = message.attachment
    conv_id_str = message.conversation.conversation_id_str

    try:
        # Use a database transaction to ensure both deletes succeed or fail together.
        with db.atomic():
            # Delete the message from the database
            message.delete_instance()

            # If there was an attachment, delete it from our records and from Minio.
            if attachment_to_delete:
                try:
                    # Delete from Minio storage first
                    minio_service.delete_file(attachment_to_delete.stored_filename)
                    # Then delete the record from our database
                    attachment_to_delete.delete_instance()
                except Exception as e:
                    # If the file cleanup fails, log it but don't fail the whole request.
                    # The user's primary goal was to delete the message.
                    print(
                        f"Error during attachment cleanup for message {message_id}: {e}"
                    )

    except Exception as e:
        print(f"Error deleting message {message_id}: {e}")
        return "Error deleting message", 500

    # Construct and broadcast the UI update
    broadcast_html = f'<div id="message-{message_id}" hx-swap-oob="delete"></div>'
    chat_manager.broadcast(conv_id_str, broadcast_html)

    return "", 200  # Use 200 OK because we are broadcasting, 204 can be ignored by HTMX


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
                json.dumps(avatar_update_payload),
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


# --- Admin Routes ---


@admin_bp.route("/users")
def list_users():
    users = User.select()
    return render_template("admin/user_list.html", users=users)


@admin_bp.route("/users/create", methods=["GET"])
def create_user_form():
    return render_template("admin/create_user.html")


@admin_bp.route("/users/create", methods=["POST"])
def create_user():
    username = request.form.get("username")
    email = request.form.get("email")
    if username and email:
        User.create(username=username, email=email)
        return redirect(url_for("admin.list_users"))
    return redirect(url_for("admin.create_user_form"))


@main_bp.route("/profile/status", methods=["PUT"])
@login_required
def update_presence_status():
    """Updates the user's presence status and broadcasts the change."""
    new_status = request.form.get("status")
    if new_status and new_status in STATUS_CLASS_MAP:
        user = g.user
        user.presence_status = new_status
        user.save()

        # Broadcast the DM list update to EVERYONE. This is a public change.
        status_class = STATUS_CLASS_MAP.get(new_status, "bg-secondary")
        dm_list_presence_html = f'<span id="status-dot-{user.id}" class="me-2 rounded-circle {status_class}" style="width: 10px; height: 10px;" hx-swap-oob="true"></span>'
        chat_manager.broadcast_to_all(dm_list_presence_html)

        # Prepare the multi-part HTTP response for the user who made the change.
        #  - The main response updates the profile header in the slide-out.
        #  - The OOB swap updates their own sidebar button.
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
    return render_template(
        "partials/message_batch.html",
        messages=messages,
        conversation_id=conversation_id,
        PAGE_SIZE=PAGE_SIZE,
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

    status_class = STATUS_CLASS_MAP.get(user.presence_status, "bg-secondary")
    presence_html = f'<span id="status-dot-{user.id}" class="me-2 rounded-circle {status_class}" style="width: 10px; height: 10px;" hx-swap-oob="true"></span>'
    chat_manager.broadcast_to_all(presence_html)

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
            attachment_file_id = data.get("attachment_file_id")

            # Confirm we have either a message or an attachment for this message
            if not chat_text and not attachment_file_id:
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
                attachment_file_id=attachment_file_id,
            )

            new_message_html = render_template(
                "partials/message.html", message=new_message
            )
            message_to_broadcast = (
                f'<div hx-swap-oob="beforeend:#message-list">{new_message_html}</div>'
            )

            chat_manager.broadcast(conv_id_str, message_to_broadcast, sender_ws=ws)

            message_for_sender = message_to_broadcast
            if parent_id:
                input_html = render_template("partials/chat_input_default.html")
                message_for_sender += f'<div id="chat-input-container" hx-swap-oob="outerHTML">{input_html}</div>'
            ws.send(message_for_sender)

            current_time = datetime.datetime.now()
            if conv_id_str in chat_manager.active_connections:
                with db.atomic():
                    for viewer_ws in chat_manager.active_connections[conv_id_str]:
                        (
                            UserConversationStatus.update(
                                last_read_timestamp=current_time
                            )
                            .where(
                                (UserConversationStatus.user == viewer_ws.user)
                                & (UserConversationStatus.conversation == conversation)
                            )
                            .execute()
                        )

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
                if getattr(member_ws, "channel_id", None) == conv_id_str:
                    continue

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
                    member_ws.send(notification_html)

                now = datetime.datetime.now()
                is_a_mention = (
                    Mention.select()
                    .where((Mention.message == new_message) & (Mention.user == member))
                    .exists()
                )

                if is_a_mention:
                    notification_payload = {
                        "type": "notification",
                        "title": f"New mention from {new_message.user.display_name or new_message.user.username}",
                        "body": new_message.content,
                        "icon": url_for(
                            "static", filename="favicon.ico", _external=True
                        ),
                        "tag": conv_id_str,
                    }
                    member_ws.send(json.dumps(notification_payload))
                    status.last_notified_timestamp = now
                    status.save()
                else:
                    should_notify = status.last_notified_timestamp is None or (
                        now - status.last_notified_timestamp
                    ) > datetime.timedelta(seconds=60)
                    if should_notify:
                        sound_payload = {"type": "sound"}
                        member_ws.send(json.dumps(sound_payload))
                        status.last_notified_timestamp = now
                        status.save()

    except Exception as e:
        print(
            f"ERROR: An exception occurred for user '{getattr(ws, 'user', 'unknown')}': {e}"
        )
    finally:
        if hasattr(ws, "user") and ws.user:
            chat_manager.set_offline(ws.user.id)
            presence_html = f'<span id="status-dot-{ws.user.id}" class="me-2 rounded-circle bg-secondary" style="width: 10px; height: 10px;" hx-swap-oob="true"></span>'
            chat_manager.broadcast_to_all(presence_html)
            chat_manager.unsubscribe(ws)
            print(f"INFO: Client connection closed for '{ws.user.username}'.")
