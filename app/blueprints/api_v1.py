import datetime
from functools import wraps

from flask import Blueprint, current_app, g, jsonify, request
from itsdangerous import URLSafeTimedSerializer
from playhouse.shortcuts import model_to_dict

from app.models import (
    Channel,
    ChannelMember,
    Conversation,
    Mention,
    Message,
    User,
    UserConversationStatus,
    Workspace,
    WorkspaceMember,
    db,
)
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
