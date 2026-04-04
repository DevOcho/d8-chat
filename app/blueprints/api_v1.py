import datetime
import os
import uuid
from functools import wraps

from flask import (
    Blueprint,
    Response,
    current_app,
    g,
    jsonify,
    request,
    stream_with_context,
)
from itsdangerous import URLSafeTimedSerializer
from PIL import Image, ImageOps
from playhouse.shortcuts import model_to_dict
from werkzeug.utils import secure_filename

from app.models import (
    Channel,
    ChannelMember,
    Conversation,
    Mention,
    Message,
    Poll,
    PollOption,
    Reaction,
    UploadedFile,
    User,
    UserConversationStatus,
    Vote,
    Workspace,
    WorkspaceMember,
    db,
)
from app.routes import get_attachments_for_messages, get_reactions_for_messages
from app.services import minio_service
from app.sso import oauth

api_v1_bp = Blueprint("api_v1", __name__)

# --- Token Utilities ---


def generate_api_token(user_id):
    """Generates a secure, stateless token valid for 30 days."""
    s = URLSafeTimedSerializer(current_app.config["SECRET_KEY"])
    return s.dumps({"user_id": user_id}, salt="api-token")


def verify_api_token(token, max_age=86400 * 30):
    """Verifies the token and returns the user_id if valid."""
    s = URLSafeTimedSerializer(current_app.config["SECRET_KEY"])
    try:
        data = s.loads(token, salt="api-token", max_age=max_age)
        return data.get("user_id")
    except Exception:
        return None


def api_token_required(f):
    """Decorator to protect API routes with Bearer token authentication."""

    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or invalid token"}), 401

        token = auth_header.split(" ", 1)[1]

        # Strip the expected prefix if it exists
        if token.startswith("d8_sec_"):
            token = token[7:]

        user_id = verify_api_token(token)
        if not user_id:
            return jsonify({"error": "Invalid or expired token"}), 401

        user = User.get_or_none(User.id == user_id)
        if not user:
            return jsonify({"error": "User not found"}), 401

        # Store user in g for the request lifecycle
        g.api_user = user
        g.user = user
        return f(*args, **kwargs)

    return decorated


def user_to_dict(user):
    """Helper to serialize the User model for JSON responses."""
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "display_name": user.display_name,
        "avatar_url": user.avatar_url,
        "presence_status": user.presence_status,
    }


def serialize_message(message, reactions_map, attachments_map):
    """Helper to serialize a Message model for JSON responses."""

    # let's start with Polls since they can be a bit complex
    poll_data = None
    if hasattr(message, "poll") and message.poll.count() > 0:
        poll = message.poll.get()

        # Fetch current API user's vote if authenticated
        user_vote = None
        if hasattr(g, "api_user") and g.api_user:
            user_vote = (
                Vote.select(Vote.option_id)
                .join(PollOption)
                .where((Vote.user == g.api_user) & (PollOption.poll == poll))
                .scalar()
            )

        poll_data = {
            "id": poll.id,
            "question": poll.question,
            "voted_option_id": user_vote,
            "options": list(
                {"id": opt.id, "text": opt.text, "count": opt.votes.count()}
                for opt in poll.options
            ),
        }

    # Handle the quoted message data, supporting both the newer quoted_message
    # field and the legacy parent_message fallback for quotes
    quoted_msg_obj = message.quoted_message or (
        message.parent_message if message.reply_type == "quote" else None
    )
    quoted_message_data = None
    if quoted_msg_obj:
        quoted_message_data = {
            "id": quoted_msg_obj.id,
            "content": quoted_msg_obj.content,
            "user": user_to_dict(quoted_msg_obj.user) if quoted_msg_obj.user else None,
        }

    return {
        "id": message.id,
        "conversation_id_str": message.conversation.conversation_id_str,
        "content": message.content,
        "created_at": message.created_at.isoformat() if message.created_at else None,
        "is_edited": message.is_edited,
        "user": user_to_dict(message.user) if message.user else None,
        "reply_type": message.reply_type,
        "parent_message_id": message.parent_message_id,
        "quoted_message_id": message.quoted_message_id,
        "quoted_message": quoted_message_data,
        "reactions": reactions_map.get(message.id, list()),
        "attachments": attachments_map.get(message.id, list()),
        "thread_reply_count": message.replies.where(
            Message.reply_type == "thread"
        ).count(),
        "last_reply_at": message.last_reply_at.isoformat()
        if message.last_reply_at
        else None,
        "poll": poll_data,
    }


# --- File Upload Utilities ---

ALLOWED_EXTENSIONS = {
    "png",
    "jpg",
    "jpeg",
    "gif",
    "pdf",
    "txt",
    "py",
    "js",
    "css",
    "html",
    "md",
    "ts",
    "zip",
}
# 50MB upload limit
MAX_CONTENT_LENGTH = 50 * 1024 * 1024


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def optimize_if_image(file_path, mime_type):
    """Resizes and compresses large images to save bandwidth and storage."""
    if not mime_type.startswith("image/"):
        return
    if "gif" in mime_type.lower():
        return  # Do not process GIFs, Pillow can break animations

    try:
        with Image.open(file_path) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail((1920, 1920), Image.Resampling.LANCZOS)
            img.save(file_path, optimize=True, quality=85)
    except Exception as e:
        current_app.logger.warning(f"Image optimization skipped/failed: {e}")


@api_v1_bp.route("/files/upload", methods=["POST"])
@api_token_required
def api_upload_file():
    """Uploads a file to Minio via the REST API and returns the file ID."""
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No selected file"}), 400

    if file and allowed_file(file.filename):
        # Secure the original filename
        original_filename = secure_filename(file.filename)

        # Generate a unique filename for storage
        file_ext = original_filename.rsplit(".", 1)[1].lower()
        stored_filename = f"{uuid.uuid4()}.{file_ext}"

        # Save the file temporarily to the server filesystem for processing
        temp_dir = os.path.join(current_app.instance_path, "temp_uploads")
        os.makedirs(temp_dir, exist_ok=True)
        temp_path = os.path.join(temp_dir, stored_filename)
        file.save(temp_path)

        # Optimize the image before checking final size and uploading
        optimize_if_image(temp_path, file.mimetype)

        # Get file size
        file_size = os.path.getsize(temp_path)
        if file_size > MAX_CONTENT_LENGTH:
            os.remove(temp_path)
            return jsonify({"error": "File exceeds maximum size limit"}), 400

        # Upload from the temporary path to Minio
        success = minio_service.upload_file(
            object_name=stored_filename, file_path=temp_path, content_type=file.mimetype
        )

        # Clean up the temporary file
        os.remove(temp_path)

        if success:
            # Create a record in our database
            new_file = UploadedFile.create(
                uploader=g.api_user,
                original_filename=original_filename,
                stored_filename=stored_filename,
                mime_type=file.mimetype,
                file_size_bytes=file_size,
            )
            return (
                jsonify(
                    {
                        "file_id": new_file.id,
                        "message": "File uploaded successfully",
                        "url": new_file.url,
                        "original_filename": new_file.original_filename,
                        "mime_type": new_file.mime_type,
                    }
                ),
                201,
            )
        else:
            return jsonify({"error": "Failed to upload file to storage"}), 500

    return jsonify({"error": "File type not allowed."}), 400


@api_v1_bp.route("/files/<int:file_id>/content", methods=["GET"])
@api_token_required
def api_get_file_content(file_id):
    """
    Proxies a file from Minio to the authenticated mobile client.
    Streams the bytes directly to avoid local dev DNS issues with presigned URLs.
    """
    try:
        uploaded_file = UploadedFile.get_by_id(file_id)
    except UploadedFile.DoesNotExist:
        return jsonify({"error": "File not found"}), 404

    # Use the internal Minio client to stream the object
    response = minio_service.minio_client_internal.get_object(
        current_app.config["MINIO_BUCKET_NAME"], uploaded_file.stored_filename
    )

    # A safe generator to ensure the Minio connection is released back to the pool
    def generate():
        try:
            for chunk in response.stream(32 * 1024):
                yield chunk
        finally:
            response.close()
            response.release_conn()

    return Response(
        stream_with_context(generate()),
        mimetype=uploaded_file.mime_type,
        headers={
            "Content-Disposition": f'inline; filename="{uploaded_file.original_filename}"',
            "Cache-Control": "private, max-age=3600",
        },
    )


# --- Auth Endpoints ---


@api_v1_bp.route("/auth/login", methods=["POST"])
def api_login():
    """Standard username/password login for the API."""
    data = request.get_json() or {}
    username = data.get("username")
    password = data.get("password")

    user = User.get_or_none((User.username == username) | (User.email == username))
    if user and user.check_password(password):
        # Prepend the requested identifier format
        token = "d8_sec_" + generate_api_token(user.id)
        return jsonify({"api_token": token, "user": user_to_dict(user)}), 200

    return jsonify({"error": "Invalid credentials"}), 401


@api_v1_bp.route("/auth/me", methods=["GET"])
@api_token_required
def get_me():
    """Returns the currently authenticated API user."""
    return jsonify({"user": user_to_dict(g.api_user)}), 200


@api_v1_bp.route("/auth/sso/exchange", methods=["POST"])
def sso_exchange():
    """Exchanges an Authentik id_token for our internal API token."""
    data = request.get_json() or {}
    id_token = data.get("id_token")

    if not id_token:
        return jsonify({"error": "Missing id_token"}), 400

    try:
        # We wrap the id_token in a dict because Authlib expects the token response object
        # We disable nonce verification here as the mobile flow might not supply the same session context
        user_info = oauth.authentik.parse_id_token({"id_token": id_token}, nonce=None)
    except Exception as e:
        current_app.logger.error(f"API SSO Exchange Error: {e}")
        return jsonify({"error": "Invalid id_token"}), 401

    sso_id = user_info.get("sub")
    email = user_info.get("email")
    display_name = user_info.get("given_name")

    if not sso_id or not email:
        return jsonify({"error": "Invalid id_token payload"}), 401

    # Match user logic (simplified from sso.py for API context)
    user = User.get_or_none(User.sso_id == sso_id)

    with db.atomic():
        if not user:
            user = User.get_or_none((User.email == email) & (User.sso_id.is_null()))
            if user:
                # Link existing user
                user.sso_id = sso_id
                user.sso_provider = "authentik"
                if display_name:
                    user.display_name = display_name
                user.save()
            else:
                # For safety, require new users to initialize their workspace via the web app first
                return jsonify(
                    {
                        "error": "User account not found. Please log into the web app once to initialize your account."
                    }
                ), 403

    token = "d8_sec_" + generate_api_token(user.id)
    return jsonify({"api_token": token, "user": user_to_dict(user)}), 200


@api_v1_bp.route("/workspaces", methods=["GET"])
@api_token_required
def get_workspaces():
    """Returns workspaces the user belongs to."""
    workspaces = (
        Workspace.select()
        .join(WorkspaceMember)
        .where(WorkspaceMember.user == g.api_user)
    )

    # We use 'only' to prevent infinite recursion traversing relationships
    results = [
        model_to_dict(w, only=[Workspace.id, Workspace.name, Workspace.created_at])
        for w in workspaces
    ]
    return jsonify({"workspaces": results}), 200


@api_v1_bp.route("/channels", methods=["GET"])
@api_token_required
def get_channels():
    """Returns channels the user is a member of with unread counts."""
    user_channels = (
        Channel.select()
        .join(ChannelMember)
        .where(ChannelMember.user == g.api_user)
        .order_by(Channel.name)
    )

    results = []
    for channel in user_channels:
        conv_id_str = f"channel_{channel.id}"
        conv = Conversation.get_or_none(conversation_id_str=conv_id_str)

        unread_count = 0
        mention_count = 0

        if conv:
            status = UserConversationStatus.get_or_none(
                user=g.api_user, conversation=conv
            )
            last_read = status.last_read_timestamp if status else datetime.datetime.min

            mention_count = (
                Mention.select()
                .join(Message)
                .where(
                    (Mention.user == g.api_user)
                    & (Message.conversation == conv)
                    & (Message.created_at > last_read)
                )
                .count()
            )

            unread_count = (
                Message.select()
                .where(
                    (Message.conversation == conv)
                    & (Message.created_at > last_read)
                    & (Message.user != g.api_user)
                )
                .count()
            )

        ch_dict = model_to_dict(
            channel,
            only=[
                Channel.id,
                Channel.name,
                Channel.topic,
                Channel.description,
                Channel.is_private,
            ],
        )
        ch_dict["unread_count"] = unread_count
        ch_dict["mention_count"] = mention_count
        results.append(ch_dict)

    return jsonify({"channels": results}), 200


@api_v1_bp.route("/dms", methods=["GET"])
@api_token_required
def get_dms():
    """Returns active DM conversations for the user with unread counts."""
    dm_convs = (
        Conversation.select()
        .join(UserConversationStatus)
        .where(
            (UserConversationStatus.user == g.api_user) & (Conversation.type == "dm")
        )
    )

    results = []
    for conv in dm_convs:
        # Extract the partner ID from the string, handling self-DMs correctly
        user_ids = [int(uid) for uid in conv.conversation_id_str.split("_")[1:]]
        partner_id = next(
            (uid for uid in user_ids if uid != g.api_user.id), g.api_user.id
        )
        partner = User.get_or_none(User.id == partner_id)

        if not partner:
            continue

        status = UserConversationStatus.get_or_none(user=g.api_user, conversation=conv)
        last_read = status.last_read_timestamp if status else datetime.datetime.min

        unread_count = (
            Message.select()
            .where(
                (Message.conversation == conv)
                & (Message.created_at > last_read)
                & (Message.user != g.api_user)
            )
            .count()
        )

        results.append(
            {
                "conversation_id_str": conv.conversation_id_str,
                "other_user": user_to_dict(partner),
                "unread_count": unread_count,
            }
        )

    return jsonify({"dms": results}), 200


@api_v1_bp.route("/conversations/<conv_id_str>/messages", methods=["GET"])
@api_token_required
def get_messages(conv_id_str):
    """Returns paginated messages for a conversation."""
    conv = Conversation.get_or_none(Conversation.conversation_id_str == conv_id_str)
    if not conv:
        return jsonify({"error": "Conversation not found"}), 404

    # Access check: is the user a part of this conversation?
    has_access = False
    if conv.type == "channel":
        channel_id = int(conv_id_str.split("_")[1])
        has_access = (
            ChannelMember.select()
            .where(
                (ChannelMember.user == g.api_user)
                & (ChannelMember.channel_id == channel_id)
            )
            .exists()
        )
    elif conv.type == "dm":
        user_ids = [int(uid) for uid in conv_id_str.split("_")[1:]]
        has_access = g.api_user.id in user_ids

    if not has_access:
        return jsonify({"error": "Access denied"}), 403

    # Pagination: fetches older messages if before_message_id is provided
    before_message_id = request.args.get("before_message_id", type=int)

    # We exclude 'thread' replies here because they only show up in the thread view or as quoted replies
    # NOTE: We must explicitly allow NULLs because standard messages have a NULL reply_type!
    query = Message.select().where(
        (Message.conversation == conv)
        & ((Message.reply_type != "thread") | (Message.reply_type.is_null()))
    )
    if before_message_id:
        query = query.where(Message.id < before_message_id)

    messages = list(query.order_by(Message.created_at.desc()).limit(30))
    # Reverse to chronological order (oldest to newest)
    messages.reverse()

    # Efficiently fetch reactions and attachments in bulk
    reactions_map = get_reactions_for_messages(messages)
    attachments_map = get_attachments_for_messages(messages)

    results = [
        serialize_message(msg, reactions_map, attachments_map) for msg in messages
    ]
    return jsonify({"messages": results}), 200


@api_v1_bp.route("/conversations/<conv_id_str>/messages", methods=["POST"])
@api_token_required
def create_message(conv_id_str):
    """Creates a new message in a conversation via REST API."""
    from flask import render_template

    from app.chat_manager import chat_manager
    from app.services import chat_service

    conv = Conversation.get_or_none(Conversation.conversation_id_str == conv_id_str)
    if not conv:
        return jsonify({"error": "Conversation not found"}), 404

    # 1. Access Check
    has_access = False
    if conv.type == "channel":
        channel_id = int(conv_id_str.split("_")[1])
        has_access = (
            ChannelMember.select()
            .where(
                (ChannelMember.user == g.api_user)
                & (ChannelMember.channel_id == channel_id)
            )
            .exists()
        )
    elif conv.type == "dm":
        user_ids = [int(uid) for uid in conv_id_str.split("_")[1:]]
        has_access = g.api_user.id in user_ids

    if not has_access:
        return jsonify({"error": "Access denied"}), 403

    # 2. Parse incoming JSON
    data = request.get_json() or {}
    content = data.get("content")
    if not content:
        return jsonify({"error": "Message content is required"}), 400

    parent_id = data.get("parent_message_id")
    reply_type = data.get("reply_type")
    quoted_message_id = data.get("quoted_message_id")
    attachment_file_ids = data.get("attachment_file_ids")

    # 3. Create the message using our centralized service
    new_message = chat_service.handle_new_message(
        sender=g.api_user,
        conversation=conv,
        chat_text=content,
        parent_id=parent_id,
        reply_type=reply_type,
        attachment_file_ids=attachment_file_ids,
        quoted_message_id=quoted_message_id,
    )

    # Prepare maps for serialization and HTML rendering
    reactions_map = get_reactions_for_messages([new_message])
    attachments_map = get_attachments_for_messages([new_message])
    message_data = serialize_message(new_message, reactions_map, attachments_map)

    # 4. Broadcast the new message to active websocket clients (Web & Mobile)
    if reply_type == "thread" and parent_id:
        parent_message = Message.get_by_id(parent_id)
        parent_reactions = get_reactions_for_messages([parent_message])
        parent_attachments = get_attachments_for_messages([parent_message])

        new_reply_html = render_template(
            "partials/message.html",
            message=new_message,
            reactions_map=reactions_map,
            attachments_map=attachments_map,
            Message=Message,
            is_in_thread_view=True,
        )
        broadcast_html = f'<div hx-swap-oob="beforeend:#thread-replies-list-{parent_id}">{new_reply_html}</div>'

        parent_html = render_template(
            "partials/message.html",
            message=parent_message,
            reactions_map=parent_reactions,
            attachments_map=parent_attachments,
            Message=Message,
            is_in_thread_view=False,
        )
        parent_oob = parent_html.replace(
            f'id="message-{parent_id}"',
            f'id="message-{parent_id}" hx-swap-oob="true"',
            1,
        )
        broadcast_html += parent_oob

        api_data = {
            "type": "new_thread_reply",
            "data": {
                "parent_message": serialize_message(
                    parent_message, parent_reactions, parent_attachments
                ),
                "reply": message_data,
            },
        }

        chat_manager.broadcast(
            conv_id_str, {"_raw_html": broadcast_html, "api_data": api_data}
        )
    else:
        new_message_html = render_template(
            "partials/message.html",
            message=new_message,
            reactions_map=reactions_map,
            attachments_map=attachments_map,
            Message=Message,
        )
        broadcast_html = (
            f'<div hx-swap-oob="beforeend:#message-list">{new_message_html}</div>'
        )

        api_data = {"type": "new_message", "data": message_data}

        chat_manager.broadcast(
            conv_id_str, {"_raw_html": broadcast_html, "api_data": api_data}
        )

    # 5. Process standard push notifications / badges for inactive users
    chat_service.send_notifications_for_new_message(new_message, g.api_user)

    # 6. Return the created message object to the REST caller
    return jsonify(message_data), 201


@api_v1_bp.route("/threads/<int:parent_message_id>", methods=["GET"])
@api_token_required
def get_thread(parent_message_id):
    """Returns a parent message and all its thread replies."""
    parent_msg = Message.get_or_none(Message.id == parent_message_id)
    if not parent_msg:
        return jsonify({"error": "Message not found"}), 404

    conv = parent_msg.conversation

    # Access check
    has_access = False
    if conv.type == "channel":
        channel_id = int(conv.conversation_id_str.split("_")[1])
        has_access = (
            ChannelMember.select()
            .where(
                (ChannelMember.user == g.api_user)
                & (ChannelMember.channel_id == channel_id)
            )
            .exists()
        )
    elif conv.type == "dm":
        user_ids = [int(uid) for uid in conv.conversation_id_str.split("_")[1:]]
        has_access = g.api_user.id in user_ids

    if not has_access:
        return jsonify({"error": "Access denied"}), 403

    # Fetch thread replies chronologically
    replies = list(
        Message.select()
        .where(
            (Message.parent_message == parent_msg) & (Message.reply_type == "thread")
        )
        .order_by(Message.created_at.asc())
    )

    all_messages = [parent_msg] + replies
    reactions_map = get_reactions_for_messages(all_messages)
    attachments_map = get_attachments_for_messages(all_messages)

    return jsonify(
        {
            "parent_message": serialize_message(
                parent_msg, reactions_map, attachments_map
            ),
            "replies": [
                serialize_message(msg, reactions_map, attachments_map)
                for msg in replies
            ],
        }
    ), 200


@api_v1_bp.route("/messages/<int:message_id>/reactions", methods=["POST"])
@api_token_required
def toggle_reaction(message_id):
    """Toggles an emoji reaction on a message for the authenticated user."""
    from app.chat_manager import chat_manager

    message = Message.get_or_none(Message.id == message_id)
    if not message:
        return jsonify({"error": "Message not found"}), 404

    data = request.get_json() or {}
    emoji = data.get("emoji", "").strip()
    if not emoji:
        return jsonify({"error": "emoji is required"}), 400

    existing = Reaction.get_or_none(
        Reaction.user == g.api_user,
        Reaction.message == message,
        Reaction.emoji == emoji,
    )
    if existing:
        existing.delete_instance()
    else:
        Reaction.create(user=g.api_user, message=message, emoji=emoji)

    reactions_map = get_reactions_for_messages([message])
    grouped_reactions = reactions_map.get(message.id, [])
    conv_id_str = message.conversation.conversation_id_str

    api_data = {
        "type": "reaction_updated",
        "data": {
            "message_id": message.id,
            "conversation_id_str": conv_id_str,
            "reactions": grouped_reactions,
        },
    }
    chat_manager.broadcast(conv_id_str, {"api_data": api_data})
    return jsonify(api_data["data"]), 200


@api_v1_bp.route("/messages/<int:message_id>", methods=["PATCH"])
@api_token_required
def edit_message(message_id):
    """Edits the content of a message. Only the author may edit."""
    from app.chat_manager import chat_manager

    message = Message.get_or_none(Message.id == message_id)
    if not message:
        return jsonify({"error": "Message not found"}), 404
    if message.user.id != g.api_user.id:
        return jsonify({"error": "Forbidden"}), 403

    data = request.get_json() or {}
    new_content = (data.get("content") or "").strip()
    if not new_content:
        return jsonify({"error": "content is required"}), 400

    message.content = new_content
    message.is_edited = True
    message.save()

    reactions_map = get_reactions_for_messages([message])
    attachments_map = get_attachments_for_messages([message])
    message_data = serialize_message(message, reactions_map, attachments_map)
    conv_id_str = message.conversation.conversation_id_str

    api_data = {"type": "message_edited", "data": message_data}
    chat_manager.broadcast(conv_id_str, {"api_data": api_data})
    return jsonify(message_data), 200


@api_v1_bp.route("/messages/<int:message_id>", methods=["DELETE"])
@api_token_required
def delete_message(message_id):
    """Deletes a message. Only the author may delete."""
    from app.chat_manager import chat_manager
    from app.services import minio_service

    message = Message.get_or_none(Message.id == message_id)
    if not message:
        return jsonify({"error": "Message not found"}), 404
    if message.user.id != g.api_user.id:
        return jsonify({"error": "Forbidden"}), 403

    attachments_to_delete = list(message.attachments)
    conv_id_str = message.conversation.conversation_id_str

    with db.atomic():
        message.delete_instance(recursive=True)
        for attachment in attachments_to_delete:
            try:
                minio_service.delete_file(attachment.stored_filename)
                attachment.delete_instance()
            except Exception as e:
                current_app.logger.warning(
                    f"Attachment cleanup failed for message {message_id}: {e}"
                )

    api_data = {
        "type": "message_deleted",
        "data": {"message_id": message_id, "conversation_id_str": conv_id_str},
    }
    chat_manager.broadcast(conv_id_str, {"api_data": api_data})
    return "", 204


# --- Conversation & Poll Endpoints ---


@api_v1_bp.route("/conversations/<conv_id_str>/members", methods=["GET"])
@api_token_required
def get_conversation_members(conv_id_str):
    """Returns a list of users in a conversation for @mention autocomplete."""
    conv = Conversation.get_or_none(Conversation.conversation_id_str == conv_id_str)
    if not conv:
        return jsonify({"error": "Conversation not found"}), 404

    if conv.type == "channel":
        channel_id = int(conv_id_str.split("_")[1])
        has_access = (
            ChannelMember.select()
            .where(
                (ChannelMember.user == g.api_user)
                & (ChannelMember.channel_id == channel_id)
            )
            .exists()
        )
        if not has_access:
            return jsonify({"error": "Access denied"}), 403

        members = (
            User.select()
            .join(ChannelMember)
            .where(ChannelMember.channel_id == channel_id)
        )
    elif conv.type == "dm":
        user_ids = [int(uid) for uid in conv_id_str.split("_")[1:]]
        if g.api_user.id not in user_ids:
            return jsonify({"error": "Access denied"}), 403
        members = User.select().where(User.id.in_(user_ids))
    else:
        return jsonify({"error": "Invalid conversation type"}), 400

    return jsonify({"members": [user_to_dict(user) for user in members]}), 200


@api_v1_bp.route("/conversations/<conv_id_str>/polls", methods=["POST"])
@api_token_required
def create_poll(conv_id_str):
    """Creates a new poll message in a conversation."""
    from flask import render_template

    from app.chat_manager import chat_manager

    conv = Conversation.get_or_none(Conversation.conversation_id_str == conv_id_str)
    if not conv:
        return jsonify({"error": "Conversation not found"}), 404

    if conv.type == "channel":
        channel_id = int(conv_id_str.split("_")[1])
        has_access = (
            ChannelMember.select()
            .where(
                (ChannelMember.user == g.api_user)
                & (ChannelMember.channel_id == channel_id)
            )
            .exists()
        )
    else:
        user_ids = [int(uid) for uid in conv_id_str.split("_")[1:]]
        has_access = g.api_user.id in user_ids

    if not has_access:
        return jsonify({"error": "Access denied"}), 403

    data = request.get_json() or {}
    question = data.get("question", "").strip()
    options = [opt.strip() for opt in data.get("options", []) if opt.strip()]

    if not question or len(options) < 2:
        return jsonify(
            {"error": "A question and at least two options are required."}
        ), 400

    with db.atomic():
        poll_message = Message.create(
            user=g.api_user, conversation=conv, content=f"[Poll]: {question}"
        )
        new_poll = Poll.create(message=poll_message, question=question)
        for option_text in options:
            PollOption.create(poll=new_poll, text=option_text)

    reactions_map = get_reactions_for_messages(list([poll_message]))
    attachments_map = get_attachments_for_messages(list([poll_message]))
    message_data = serialize_message(poll_message, reactions_map, attachments_map)

    new_message_html = render_template(
        "partials/message.html",
        message=poll_message,
        reactions_map=reactions_map,
        attachments_map=attachments_map,
        Message=Message,
    )
    broadcast_html = (
        f'<div hx-swap-oob="beforeend:#message-list">{new_message_html}</div>'
    )

    api_data = {"type": "new_message", "data": message_data}
    chat_manager.broadcast(
        conv_id_str, {"_raw_html": broadcast_html, "api_data": api_data}
    )

    return jsonify(message_data), 201


@api_v1_bp.route("/polls/<int:poll_id>/vote", methods=["POST"])
@api_token_required
def api_vote_on_poll(poll_id):
    """Casts or changes a vote on a poll."""
    from flask import render_template

    from app.blueprints.polls import get_poll_context
    from app.chat_manager import chat_manager

    poll = Poll.get_or_none(Poll.id == poll_id)
    if not poll:
        return jsonify({"error": "Poll not found"}), 404

    data = request.get_json() or {}
    option_id = data.get("option_id")

    option = PollOption.get_or_none(PollOption.id == option_id)
    if not option or option.poll_id != poll.id:
        return jsonify({"error": "Invalid option"}), 400

    message = poll.message
    conv_id_str = message.conversation.conversation_id_str

    with db.atomic():
        existing_vote = (
            Vote.select()
            .join(PollOption)
            .where((Vote.user == g.api_user) & (PollOption.poll == poll))
            .first()
        )

        if existing_vote:
            if existing_vote.option.id == option.id:
                existing_vote.delete_instance()  # Un-vote
            else:
                existing_vote.delete_instance()  # Switch vote
                Vote.create(user=g.api_user, option=option)
        else:
            Vote.create(user=g.api_user, option=option)

    reactions_map = get_reactions_for_messages(list([message]))
    attachments_map = get_attachments_for_messages(list([message]))
    message_data = serialize_message(message, reactions_map, attachments_map)

    # Web client OOB HTML update
    poll_context = get_poll_context(poll, g.api_user)
    broadcast_html = render_template(
        "partials/message_poll_oob_update.html", poll_context=poll_context
    )

    # Tell mobile clients the message was edited so they re-render the entire poll block
    api_data = {"type": "message_edited", "data": message_data}

    chat_manager.broadcast(
        conv_id_str, {"_raw_html": broadcast_html, "api_data": api_data}
    )

    return jsonify(message_data), 200
