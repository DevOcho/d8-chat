import datetime
import hmac
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
from flask_limiter.util import get_remote_address
from itsdangerous import URLSafeTimedSerializer
from PIL import Image, ImageOps
from playhouse.shortcuts import model_to_dict
from werkzeug.utils import secure_filename

from app import limiter, login_username_key
from app.access import user_has_conversation_access
from app.conversation_id import parse_conversation_id
from app.models import (
    Channel,
    ChannelMember,
    Conversation,
    Mention,
    Message,
    MessageAttachment,
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
    utc_now,
)
from app.routes import get_attachments_for_messages, get_reactions_for_messages
from app.services import minio_service
from app.services.upload_validation import (
    ALLOWED_EXTENSIONS,
    AVATAR_EXTENSIONS,
    ValidationError,
    validate_upload,
)
from app.sso import oauth

api_v1_bp = Blueprint("api_v1", __name__)

# Whitelist of redirect URIs accepted by /auth/sso/exchange. Mobile clients use
# the custom scheme; web clients exchange via the regular SSO callback. Anything
# outside this set is rejected to prevent OAuth code interception.
ALLOWED_SSO_REDIRECT_URIS = frozenset({"d8chat://auth/callback"})


def _api_user_key():
    """
    Per-user rate-limit key for authenticated API endpoints.

    Used as the `key_func` on `@limiter.limit` decorators that are placed
    *below* `@api_token_required`, so `g.api_user` is populated by the time
    this runs. Falls back to the client's remote address if no user is
    attached — that fallback only matters for misconfigured routes since the
    auth decorator runs first.
    """
    user = getattr(g, "api_user", None)
    if user is not None:
        return f"api_user:{user.id}"
    return get_remote_address()


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

        user = User.get_active_by_id(user_id)
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

# 50MB upload limit
MAX_CONTENT_LENGTH = 50 * 1024 * 1024


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
@limiter.limit("20 per minute", key_func=_api_user_key)
def api_upload_file():
    """Uploads a file to Minio via the REST API and returns the file ID."""
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No selected file"}), 400

    original_filename = secure_filename(file.filename)
    if "." not in original_filename:
        return jsonify({"error": "File must have an extension."}), 400

    file_ext = original_filename.rsplit(".", 1)[1].lower()
    if file_ext not in ALLOWED_EXTENSIONS:
        return jsonify({"error": "File type not allowed."}), 400

    stored_filename = f"{uuid.uuid4()}.{file_ext}"
    temp_dir = os.path.join(current_app.instance_path, "temp_uploads")
    os.makedirs(temp_dir, exist_ok=True)
    temp_path = os.path.join(temp_dir, stored_filename)
    file.save(temp_path)

    try:
        # Sniff actual content; reject if the bytes don't match the extension.
        # Persisted MIME is the libmagic-detected one — never the client value.
        try:
            validated = validate_upload(temp_path, original_filename)
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400

        # Re-encode images to strip embedded payloads / EXIF / oversize.
        optimize_if_image(temp_path, validated.sniffed_mime)

        file_size = os.path.getsize(temp_path)
        if file_size > MAX_CONTENT_LENGTH:
            return jsonify({"error": "File exceeds maximum size limit"}), 400

        success = minio_service.upload_file(
            object_name=stored_filename,
            file_path=temp_path,
            content_type=validated.sniffed_mime,
        )
        if not success:
            return jsonify({"error": "Failed to upload file to storage"}), 500

        new_file = UploadedFile.create(
            uploader=g.api_user,
            original_filename=original_filename,
            stored_filename=stored_filename,
            mime_type=validated.sniffed_mime,
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
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def _user_can_access_file(user, uploaded_file):
    """
    Returns True if `user` is allowed to read `uploaded_file`. Access is granted
    when the user uploaded the file, when the file is set as any user's avatar
    (avatars are public), or when the file is attached to a message in a
    conversation the user is a member of.
    """
    if uploaded_file.uploader_id == user.id:
        return True

    if User.select().where(User.avatar == uploaded_file).exists():
        return True

    attached_message_ids = MessageAttachment.select(MessageAttachment.message).where(
        MessageAttachment.attachment == uploaded_file
    )
    conversations = (
        Conversation.select()
        .join(Message, on=(Message.conversation == Conversation.id))
        .where(Message.id.in_(attached_message_ids))
        .distinct()
    )
    for conv in conversations:
        try:
            parsed = parse_conversation_id(conv.conversation_id_str)
        except ValueError:
            continue
        if parsed.type == "channel":
            if (
                ChannelMember.select()
                .where(
                    (ChannelMember.user == user)
                    & (ChannelMember.channel_id == parsed.channel_id)
                )
                .exists()
            ):
                return True
        elif parsed.type == "dm":
            if user.id in parsed.user_ids:
                return True

    return False


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

    if not _user_can_access_file(g.api_user, uploaded_file):
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


@api_v1_bp.route("/app-config", methods=["GET"])
@limiter.limit("60 per minute")
def get_app_config():
    """Returns server configuration and SSO details for the mobile app launch screen."""
    sso_enabled = bool(current_app.config.get("OIDC_CLIENT_ID"))
    sso_auth_url = None

    if sso_enabled:
        redirect_uri = next(iter(ALLOWED_SSO_REDIRECT_URIS))
        try:
            url, _state, *_ = oauth.authentik.create_authorization_url(redirect_uri)
            sso_auth_url = url
        except Exception as e:
            current_app.logger.warning(f"Could not generate SSO auth URL: {e}")

    return jsonify(
        {
            "server_name": current_app.config["BRAND_SERVER_NAME"],
            "logo_url": current_app.config.get("BRAND_LOGO_URL"),
            "primary_color": current_app.config["BRAND_PRIMARY_COLOR"],
            "password_auth_enabled": True,
            "sso_enabled": sso_enabled,
            "sso_provider_name": (
                current_app.config["BRAND_SSO_PROVIDER_NAME"] if sso_enabled else None
            ),
            "sso_auth_url": sso_auth_url,
            "version": "1.0.0",
        }
    ), 200


# --- Auth Endpoints ---


@api_v1_bp.route("/auth/login", methods=["POST"])
@limiter.limit("5 per minute; 50 per hour")
@limiter.limit("10 per minute; 50 per hour", key_func=login_username_key)
def api_login():
    """Standard username/password login for the API."""
    data = request.get_json() or {}
    username = data.get("username")
    password = data.get("password")

    user = User.get_or_none((User.username == username) | (User.email == username))
    # Treat deactivated accounts the same as wrong credentials — same response
    # body and status code so the API doesn't reveal account-status info.
    if user and user.is_active and user.check_password(password):
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
@limiter.limit("20 per minute")
def sso_exchange():
    """Exchanges an OIDC authorization code for our internal API token."""
    from app.sso import _create_or_link_sso_user

    data = request.get_json() or dict()
    code = data.get("code")
    redirect_uri = data.get("redirect_uri")

    if not code or not redirect_uri:
        return jsonify({"error": "Missing code or redirect_uri"}), 400

    if redirect_uri not in ALLOWED_SSO_REDIRECT_URIS:
        current_app.logger.warning(
            f"SSO exchange rejected: disallowed redirect_uri {redirect_uri!r}"
        )
        return jsonify({"error": "redirect_uri is not allowed"}), 400

    try:
        # Fetch token using the authorization code and secret (server-side)
        token_response = oauth.authentik.fetch_access_token(
            redirect_uri=redirect_uri, code=code
        )
        # Parse the ID token claims from the response
        user_info = oauth.authentik.parse_id_token(token_response, nonce=None)
    except Exception as e:
        current_app.logger.error(f"API SSO Exchange Error: {e}")
        return jsonify({"error": "Authorization code is invalid or has expired"}), 401

    sso_id = user_info.get("sub")
    email = user_info.get("email")
    display_name = user_info.get("given_name")

    if not sso_id or not email:
        return jsonify({"error": "Invalid id_token payload"}), 401

    # Generate a safe base username from the email
    base_username = email.split("@")[0].lower().replace(".", "_")

    with db.atomic():
        # Leverage existing web SSO logic to link/create the user and auto-join channels
        user = _create_or_link_sso_user(sso_id, email, base_username, display_name)

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
        try:
            user_ids = parse_conversation_id(conv.conversation_id_str).user_ids
        except ValueError:
            continue
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

    try:
        parsed = parse_conversation_id(conv_id_str)
    except ValueError:
        return jsonify({"error": "Malformed conversation id"}), 400

    if not user_has_conversation_access(g.api_user, parsed):
        return jsonify({"error": "Access denied"}), 403

    # Pagination: fetches older messages if before_message_id is provided
    # Or fetches a window around a specific message if around_message_id is provided
    before_message_id = request.args.get("before_message_id", type=int)
    around_message_id = request.args.get("around_message_id", type=int)

    base_condition = (Message.conversation == conv) & (
        (Message.reply_type != "thread") | (Message.reply_type.is_null())
    )

    messages = list()

    if around_message_id:
        # Fetch 15 messages before the target
        msgs_before = list(
            Message.select()
            .where(base_condition & (Message.id < around_message_id))
            .order_by(Message.created_at.desc())
            .limit(15)
        )
        msgs_before.reverse()

        # Fetch the target message itself
        target_msg = Message.get_or_none(Message.id == around_message_id)

        # Fetch 15 messages after the target
        msgs_after = list(
            Message.select()
            .where(base_condition & (Message.id > around_message_id))
            .order_by(Message.created_at.asc())
            .limit(15)
        )

        messages.extend(msgs_before)
        if target_msg:
            messages.append(target_msg)
        messages.extend(msgs_after)
    else:
        query = Message.select().where(base_condition)
        if before_message_id:
            query = query.where(Message.id < before_message_id)

        fetched_msgs = list(query.order_by(Message.created_at.desc()).limit(30))
        # Reverse to chronological order (oldest to newest)
        fetched_msgs.reverse()
        messages.extend(fetched_msgs)

    # Efficiently fetch reactions and attachments in bulk
    reactions_map = get_reactions_for_messages(messages)
    attachments_map = get_attachments_for_messages(messages)

    results = list(
        (serialize_message(msg, reactions_map, attachments_map) for msg in messages)
    )
    return jsonify({"messages": results}), 200


@api_v1_bp.route("/conversations/<conv_id_str>/messages", methods=["POST"])
@api_token_required
@limiter.limit("60 per minute", key_func=_api_user_key)
def create_message(conv_id_str):
    """Creates a new message in a conversation via REST API."""
    from flask import render_template

    from app.chat_manager import chat_manager
    from app.services import chat_service

    conv = Conversation.get_or_none(Conversation.conversation_id_str == conv_id_str)
    if not conv:
        return jsonify({"error": "Conversation not found"}), 404

    try:
        parsed = parse_conversation_id(conv_id_str)
    except ValueError:
        return jsonify({"error": "Malformed conversation id"}), 400

    if not user_has_conversation_access(g.api_user, parsed):
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

    try:
        parsed = parse_conversation_id(conv.conversation_id_str)
    except ValueError:
        return jsonify({"error": "Malformed conversation id"}), 400

    if not user_has_conversation_access(g.api_user, parsed):
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
@limiter.limit("60 per minute", key_func=_api_user_key)
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
@limiter.limit("30 per minute", key_func=_api_user_key)
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

    try:
        parsed = parse_conversation_id(conv_id_str)
    except ValueError:
        return jsonify({"error": "Malformed conversation id"}), 400

    if not user_has_conversation_access(g.api_user, parsed):
        return jsonify({"error": "Access denied"}), 403

    if parsed.type == "channel":
        members = (
            User.select()
            .join(ChannelMember)
            .where(ChannelMember.channel_id == parsed.channel_id)
        )
    elif parsed.type == "dm":
        members = User.select().where(User.id.in_(list(parsed.user_ids)))
    else:
        return jsonify({"error": "Invalid conversation type"}), 400

    return jsonify({"members": [user_to_dict(user) for user in members]}), 200


@api_v1_bp.route("/conversations/<conv_id_str>/read", methods=["POST"])
@api_token_required
def mark_conversation_read(conv_id_str):
    """Marks a conversation as read and broadcasts the cleared state to the user's other sessions."""
    from app.chat_manager import chat_manager

    conv = Conversation.get_or_none(Conversation.conversation_id_str == conv_id_str)
    if not conv:
        return jsonify({"error": "Conversation not found"}), 404

    try:
        parsed = parse_conversation_id(conv_id_str)
    except ValueError:
        return jsonify({"error": "Malformed conversation id"}), 400

    if not user_has_conversation_access(g.api_user, parsed):
        return jsonify({"error": "Access denied"}), 403

    status, _ = UserConversationStatus.get_or_create(user=g.api_user, conversation=conv)
    status.last_read_timestamp = utc_now()
    status.save()

    chat_manager.send_to_user(
        g.api_user.id,
        {
            "api_data": {
                "type": "unread_updated",
                "data": {
                    "conversation_id_str": conv_id_str,
                    "unread_count": 0,
                    "is_mention": False,
                },
            }
        },
    )

    return "", 204


@api_v1_bp.route("/conversations/<conv_id_str>/polls", methods=["POST"])
@api_token_required
@limiter.limit("10 per minute", key_func=_api_user_key)
def create_poll(conv_id_str):
    """Creates a new poll message in a conversation."""
    from flask import render_template

    from app.chat_manager import chat_manager

    conv = Conversation.get_or_none(Conversation.conversation_id_str == conv_id_str)
    if not conv:
        return jsonify({"error": "Conversation not found"}), 404

    try:
        parsed = parse_conversation_id(conv_id_str)
    except ValueError:
        return jsonify({"error": "Malformed conversation id"}), 400

    if not user_has_conversation_access(g.api_user, parsed):
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
@limiter.limit("30 per minute", key_func=_api_user_key)
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


# --- Search Endpoint ---


@api_v1_bp.route("/search", methods=["GET"])
@api_token_required
def api_search():
    """Searches messages, channels, and people across the workspace."""
    from peewee import JOIN, fn

    from app.blueprints.search import (
        _get_accessible_conversations,
        _get_message_context,
    )

    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "query parameter 'q' is required"}), 400

    limit = request.args.get("limit", 20, type=int)
    if limit > 50:
        limit = 50

    # 1. Search Messages
    accessible_convs_query = _get_accessible_conversations(g.api_user)
    message_query = (
        Message.select(Message, User, Conversation)
        .join(User)
        .switch(Message)
        .join(Conversation)
        .where(
            Message.content.ilike(f"%{q}%"),
            Message.conversation.in_(accessible_convs_query),
        )
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    msg_results = list(message_query)
    msg_context = _get_message_context(msg_results, g.api_user)

    messages_out = list()
    for msg in msg_results:
        raw_name = msg_context.get(msg.id, "Unknown")
        # _get_message_context adds '# ' to channels; we strip it to match the mobile spec
        clean_name = raw_name.lstrip("# ")

        messages_out.append(
            {
                "id": msg.id,
                "content": msg.content,
                "created_at": msg.created_at.isoformat() if msg.created_at else None,
                "conversation_id_str": msg.conversation.conversation_id_str,
                "conversation_name": clean_name,
                "user": user_to_dict(msg.user),
            }
        )

    # 2. Search Channels
    user_private_channels_subquery = (
        Channel.select(Channel.id)
        .join(ChannelMember)
        .where((ChannelMember.user == g.api_user) & Channel.is_private)
    )
    channel_query = (
        Channel.select(Channel, fn.COUNT(ChannelMember.id).alias("member_count"))
        .join(ChannelMember, JOIN.LEFT_OUTER)
        .where(
            (Channel.name.ilike(f"%{q}%"))
            & ((~Channel.is_private) | (Channel.id.in_(user_private_channels_subquery)))
        )
        .group_by(Channel.id)
        .limit(limit)
    )

    channels_out = list()
    for ch in channel_query:
        channels_out.append(
            {
                "id": ch.id,
                "name": ch.name,
                "description": ch.description,
                "is_private": ch.is_private,
                "conv_id": f"channel_{ch.id}",
                "member_count": ch.member_count,
            }
        )

    # 3. Search People
    user_query = (
        User.select()
        .where((User.username.ilike(f"%{q}%")) | (User.display_name.ilike(f"%{q}%")))
        .limit(limit)
    )

    # Pre-fetch existing DMs to accurately map dm_conv_id
    existing_dms = (
        Conversation.select()
        .join(UserConversationStatus)
        .where(
            (Conversation.type == "dm") & (UserConversationStatus.user == g.api_user)
        )
    )
    dm_set = set(c.conversation_id_str for c in existing_dms)

    people_out = list()
    for u in user_query:
        # Construct the expected dm_conv_id string (IDs sorted ascending)
        user_ids = list((g.api_user.id, u.id))
        user_ids.sort()
        expected_dm_str = f"dm_{user_ids[0]}_{user_ids[1]}"
        dm_conv_id = expected_dm_str if expected_dm_str in dm_set else None

        people_out.append(
            {
                "id": u.id,
                "username": u.username,
                "display_name": u.display_name,
                "avatar_url": u.avatar_url,
                "presence_status": u.presence_status,
                "dm_conv_id": dm_conv_id,
            }
        )

    return jsonify(
        {
            "query": q,
            "messages": messages_out,
            "channels": channels_out,
            "people": people_out,
        }
    ), 200


# --- User Profile Endpoints ---


@api_v1_bp.route("/users/me", methods=["PATCH"])
@api_token_required
def update_me():
    """Updates the authenticated user's profile."""
    data = request.get_json() or dict()
    display_name = data.get("display_name")
    if display_name is not None:
        g.api_user.display_name = display_name
        g.api_user.save()
    return jsonify(user_to_dict(g.api_user)), 200


@api_v1_bp.route("/users/me/avatar", methods=["POST"])
@api_token_required
@limiter.limit("10 per minute", key_func=_api_user_key)
def update_avatar():
    """Uploads and sets a new avatar for the authenticated user."""
    from app.chat_manager import chat_manager

    file = request.files.get("file")
    if not file or file.filename == "":
        return jsonify({"error": "No file provided"}), 400

    original_filename = secure_filename(file.filename)
    stored_filename = f"{uuid.uuid4()}.png"
    temp_dir = os.path.join(current_app.instance_path, "temp_uploads")
    os.makedirs(temp_dir, exist_ok=True)
    temp_path = os.path.join(temp_dir, stored_filename)
    file.save(temp_path)

    try:
        # Avatars must be raster images; sniff the bytes to reject anything
        # else even if the extension and Content-Type lie.
        try:
            validate_upload(
                temp_path,
                original_filename,
                allowed_extensions=AVATAR_EXTENSIONS,
            )
        except ValidationError as exc:
            return jsonify({"error": str(exc)}), 400

        # Re-encode to PNG. If Pillow can't open it, refuse — don't fall
        # through to storing the raw bytes as before.
        try:
            with Image.open(temp_path) as img:
                img = ImageOps.exif_transpose(img)
                img.thumbnail((1920, 1920), Image.Resampling.LANCZOS)
                img.save(temp_path, format="PNG", optimize=True)
        except Exception as exc:
            current_app.logger.warning(f"Avatar re-encode failed: {exc}")
            return jsonify({"error": "Could not process image."}), 400

        file_size = os.path.getsize(temp_path)

        success = minio_service.upload_file(
            object_name=stored_filename, file_path=temp_path, content_type="image/png"
        )
        if not success:
            return jsonify({"error": "Failed to upload file to storage"}), 500

        old_avatar_file = g.api_user.avatar
        new_file = UploadedFile.create(
            uploader=g.api_user,
            original_filename=original_filename,
            stored_filename=stored_filename,
            mime_type="image/png",
            file_size_bytes=file_size,
        )
        g.api_user.avatar = new_file
        g.api_user.save()

        if old_avatar_file:
            try:
                minio_service.delete_file(old_avatar_file.stored_filename)
                old_avatar_file.delete_instance()
            except Exception as e:
                current_app.logger.warning(f"Failed to delete old avatar: {e}")

        chat_manager.broadcast_to_all(
            {
                "type": "avatar_update",
                "user_id": g.api_user.id,
                "avatar_url": g.api_user.avatar_url,
            }
        )

        return jsonify({"avatar_url": g.api_user.avatar_url}), 200
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


HELPDESK_BOT_USERNAME = "helpdesk-bot"


@api_v1_bp.route("/internal/notify", methods=["POST"])
@limiter.limit("120 per minute", key_func=get_remote_address)
def internal_notify():
    """Service-to-service hook that posts a message into a channel.

    Auth: shared secret in the ``X-Internal-Key`` header, compared in
    constant time against ``INTERNAL_NOTIFY_KEY`` from config. There is no
    logged-in user; the message is authored by a dedicated ``helpdesk-bot``
    User row (created by migration 0003 / ``init_db.py``).

    Body:
        ``{"channel_name": "<name>", "message": "<text>"}``

    Behaviour mirrors the user-facing message-create path so the message
    is persisted, mention/hashtag extraction runs, the message is
    broadcast over the WebSocket Pub/Sub layer, and unread
    badges/notifications fire for offline channel members.
    """
    from flask import render_template

    from app.chat_manager import chat_manager
    from app.services import chat_service

    expected_key = current_app.config.get("INTERNAL_NOTIFY_KEY")
    provided_key = request.headers.get("X-Internal-Key", "")
    if not expected_key or not hmac.compare_digest(expected_key, provided_key):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    channel_name = data.get("channel_name")
    message_text = data.get("message")
    if (
        not isinstance(channel_name, str)
        or not channel_name
        or not isinstance(message_text, str)
        or not message_text
    ):
        return jsonify({"error": "channel_name and message are required"}), 400

    # Channel names are unique per workspace, not globally. For the
    # current single-workspace deployment this picks the only match;
    # if multiple workspaces ever exist we'd extend the payload with a
    # workspace identifier rather than guess here.
    channel = (
        Channel.select()
        .where(Channel.name == channel_name)
        .order_by(Channel.id)
        .first()
    )
    if channel is None:
        return jsonify({"error": "Channel not found"}), 404

    conv_id_str = f"channel_{channel.id}"
    conv = Conversation.get_or_none(Conversation.conversation_id_str == conv_id_str)
    if conv is None:
        return jsonify({"error": "Channel conversation missing"}), 500

    bot_user = User.get_or_none(User.username == HELPDESK_BOT_USERNAME)
    if bot_user is None:
        return jsonify({"error": "Helpdesk bot user not provisioned"}), 500

    new_message = chat_service.handle_new_message(
        sender=bot_user,
        conversation=conv,
        chat_text=message_text,
    )

    reactions_map = get_reactions_for_messages([new_message])
    attachments_map = get_attachments_for_messages([new_message])
    message_data = serialize_message(new_message, reactions_map, attachments_map)

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

    chat_service.send_notifications_for_new_message(new_message, bot_user)

    return jsonify({"ok": True}), 200


@api_v1_bp.route("/users/me/presence", methods=["POST"])
@api_token_required
def update_presence():
    """Updates the user's presence status and broadcasts the change."""
    from app.chat_manager import chat_manager

    data = request.get_json() or dict()
    status = data.get("status")
    valid_statuses = list(("online", "away", "busy"))

    if status not in valid_statuses:
        return jsonify({"error": "Invalid status"}), 400

    g.api_user.presence_status = status
    g.api_user.save()

    presence_class_map = {
        "online": "presence-online",
        "away": "presence-away",
        "busy": "presence-busy",
    }
    status_class = presence_class_map.get(status)

    chat_manager.broadcast_to_all(
        {
            "type": "presence_update",
            "user_id": g.api_user.id,
            "status_class": status_class,
            "status": status,
        }
    )

    return jsonify({"status": status}), 200
