from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    session,
    g,
    make_response,
)
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
)
from .sso import oauth  # Import the oauth object
import functools
import secrets
from . import sock
from .chat_manager import chat_manager
import json
from peewee import IntegrityError, fn, JOIN  # <-- [THE FIX] IMPORT JOIN
import re
import datetime
from functools import reduce
import operator
import markdown

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
    return render_template("profile.html", user=g.user, theme=g.user.theme)


# --- CHAT INTERFACE ROUTES ---
# Replace the existing chat_interface function with this one


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


@main_bp.route("/chat/channel/<int:channel_id>")
@login_required
def get_channel_chat(channel_id):
    channel = Channel.get_or_none(id=channel_id)
    if not channel:
        return "Channel not found", 404

    is_member = (
        ChannelMember.select()
        .where((ChannelMember.user == g.user) & (ChannelMember.channel == channel))
        .exists()
    )

    if channel.is_private and not is_member:
        return "You are not a member of this private channel.", 403

    add_to_sidebar_html = ""
    # [THE FIX] If user is joining a public channel for the first time, add it to their sidebar
    if not channel.is_private and not is_member:
        ChannelMember.create(user=g.user, channel=channel)
        add_to_sidebar_html = render_template(
            "partials/channel_list_item.html", channel=channel
        )

    conv_id_str = f"channel_{channel_id}"
    conversation, _ = Conversation.get_or_create(
        conversation_id_str=conv_id_str, defaults={"type": "channel"}
    )
    status, _ = UserConversationStatus.get_or_create(
        user=g.user, conversation=conversation
    )
    last_read_timestamp = status.last_read_timestamp
    status.last_read_timestamp = datetime.datetime.now()
    status.save()

    messages = list(
        Message.select()
        .where(Message.conversation == conversation)
        .order_by(Message.created_at.desc())
        .limit(PAGE_SIZE)
    )
    messages.reverse()
    reactions_map = get_reactions_for_messages(messages)
    members_count = (
        ChannelMember.select().where(ChannelMember.channel == channel).count()
    )

    # [THE FIX] Construct the response as a string of HTML fragments
    header_html_content = render_template(
        "partials/channel_header.html", channel=channel, members_count=members_count
    )
    header_html = f'<div id="chat-header-container" hx-swap-oob="true">{header_html_content}</div>'

    # This is the main content for the target
    messages_html = render_template(
        "partials/channel_messages.html",
        channel=channel,
        messages=messages,
        last_read_timestamp=last_read_timestamp,
        mention_message_ids=set(),
        PAGE_SIZE=PAGE_SIZE,
        reactions_map=reactions_map,
    )

    clear_badge_html = render_template(
        "partials/clear_badge.html",
        conv_id_str=conv_id_str,
        hx_get_url=url_for("main.get_channel_chat", channel_id=channel.id),
        link_text=f"# {channel.name}",
    )

    full_response = messages_html + header_html + clear_badge_html + add_to_sidebar_html
    response = make_response(full_response)
    response.headers["HX-Trigger"] = "load-chat-history"
    return response


@main_bp.route("/chat/channel/<int:channel_id>/details", methods=["GET"])
@login_required
def get_channel_details(channel_id):
    """Renders the channel details shell with the default 'About' tab."""
    channel = Channel.get_or_none(id=channel_id)
    if not channel:
        return "Channel not found", 404

    current_user_membership = ChannelMember.get_or_none(user=g.user, channel=channel)
    if not current_user_membership:
        return "You are not a member of this channel.", 403

    # Data needed for the 'About' tab (the default view)
    admins = list(
        ChannelMember.select().where(
            (ChannelMember.channel == channel) & (ChannelMember.role == "admin")
        )
    )
    members_count = (
        ChannelMember.select().where(ChannelMember.channel == channel).count()
    )

    return render_template(
        "partials/channel_details.html",
        channel=channel,
        admins=admins,
        members_count=members_count,
        current_user_membership=current_user_membership,
    )


@main_bp.route("/chat/channel/<int:channel_id>/details/about", methods=["GET"])
@login_required
def get_channel_details_about_tab(channel_id):
    """Renders the content for the 'About' tab."""
    channel = Channel.get_by_id(channel_id)
    admins = list(
        ChannelMember.select().where(
            (ChannelMember.channel == channel) & (ChannelMember.role == "admin")
        )
    )
    current_user_membership = ChannelMember.get_or_none(user=g.user, channel=channel)
    return render_template(
        "partials/channel_details_tab_about.html",
        channel=channel,
        admins=admins,
        current_user_membership=current_user_membership,
    )


@main_bp.route("/chat/channel/<int:channel_id>/details/members", methods=["GET"])
@login_required
def get_channel_details_members_tab(channel_id):
    """
    Renders the content for the 'Members' tab, showing current members.
    The list of users to add is now loaded via a separate search request.
    """
    channel = Channel.get_by_id(channel_id)
    current_user_membership = ChannelMember.get_or_none(user=g.user, channel=channel)

    if not current_user_membership:
        return "Unauthorized", 403

    # We ONLY need to query for the current admins and members for the initial load.
    admins = list(
        ChannelMember.select()
        .join(User)
        .where((ChannelMember.channel == channel) & (ChannelMember.role == "admin"))
        .order_by(User.username)
    )
    members = list(
        ChannelMember.select()
        .join(User)
        .where((ChannelMember.channel == channel) & (ChannelMember.role == "member"))
        .order_by(User.username)
    )

    # We no longer pre-load the list of users to invite.
    return render_template(
        "partials/channel_details_tab_members.html",
        channel=channel,
        admins=admins,
        members=members,
        current_user_membership=current_user_membership,
    )


@main_bp.route("/chat/channel/<int:channel_id>/details/settings", methods=["GET"])
@login_required
def get_channel_details_settings_tab(channel_id):
    """Renders the content for the 'Settings' tab."""
    channel = Channel.get_by_id(channel_id)
    current_user_membership = ChannelMember.get_or_none(user=g.user, channel=channel)
    if not current_user_membership or current_user_membership.role != "admin":
        return "Unauthorized", 403
    return render_template(
        "partials/channel_details_tab_settings.html", channel=channel
    )


@main_bp.route("/chat/channel/<int:channel_id>/about", methods=["GET"])
@login_required
def get_channel_details_about_display(channel_id):
    """Returns the read-only view of the channel 'About' section."""
    channel = Channel.get_by_id(channel_id)
    current_user_membership = ChannelMember.get_or_none(user=g.user, channel=channel)
    return render_template(
        "partials/channel_details_about_display.html",
        channel=channel,
        current_user_membership=current_user_membership,
    )


@main_bp.route("/chat/channel/<int:channel_id>/about/edit", methods=["GET"])
@login_required
def get_channel_about_form(channel_id):
    """Returns the form for editing channel details."""
    channel = Channel.get_by_id(channel_id)
    membership = ChannelMember.get_or_none(user=g.user, channel=channel)
    if not membership or membership.role != "admin":
        return "Unauthorized", 403

    return render_template("partials/channel_details_about_form.html", channel=channel)


@main_bp.route("/chat/channel/<int:channel_id>/about", methods=["PUT"])
@login_required
def update_channel_about(channel_id):
    """Processes the submission of the channel details edit form."""
    channel = Channel.get_by_id(channel_id)
    membership = ChannelMember.get_or_none(user=g.user, channel=channel)
    if not membership or membership.role != "admin":
        return "Unauthorized", 403

    channel.topic = request.form.get("topic")
    channel.description = request.form.get("description")
    channel.save()

    # We need to send back the updated "About" display section
    current_user_membership = ChannelMember.get_or_none(user=g.user, channel=channel)
    display_html = render_template(
        "partials/channel_details_about_display.html",
        channel=channel,
        current_user_membership=current_user_membership,
    )

    # And an OOB swap for the main chat header
    members_count = (
        ChannelMember.select().where(ChannelMember.channel == channel).count()
    )
    header_html = render_template(
        "partials/channel_header.html", channel=channel, members_count=members_count
    )

    chat_manager.broadcast(f"channel_{channel.id}", header_html)

    return display_html


@main_bp.route("/chat/channel/<int:channel_id>/members", methods=["POST"])
@login_required
def add_channel_member(channel_id):
    """Processes adding a new member to a channel."""
    user_id_to_add = request.form.get("user_id", type=int)
    channel = Channel.get_or_none(id=channel_id)
    if not user_id_to_add or not channel:
        return "Invalid request", 400
    current_user_membership = ChannelMember.get_or_none(user=g.user, channel=channel)
    if not current_user_membership:
        return "You are not a member of this channel.", 403
    if channel.invites_restricted_to_admins and current_user_membership.role != "admin":
        return "Only admins can invite new members to this channel.", 403
    ChannelMember.get_or_create(user_id=user_id_to_add, channel_id=channel_id)
    if user_id_to_add in chat_manager.all_clients:
        try:
            recipient_ws = chat_manager.all_clients[user_id_to_add]
            new_channel_html = render_template(
                "partials/channel_list_item.html", channel=channel
            )
            recipient_ws.send(new_channel_html)
        except Exception as e:
            print(f"Could not send real-time channel add to user {user_id_to_add}: {e}")

    # Re-render the members tab for the admin.
    admins = list(
        ChannelMember.select()
        .join(User)
        .where((ChannelMember.channel == channel) & (ChannelMember.role == "admin"))
        .order_by(User.username)
    )
    members = list(
        ChannelMember.select()
        .join(User)
        .where((ChannelMember.channel == channel) & (ChannelMember.role == "member"))
        .order_by(User.username)
    )
    subquery = ChannelMember.select(ChannelMember.user_id).where(
        ChannelMember.channel_id == channel_id
    )
    users_to_invite = (
        User.select()
        .join(WorkspaceMember)
        .where(User.id.not_in(subquery), WorkspaceMember.workspace == channel.workspace)
    )

    # This is the HTML for the main swap target (the tab content)
    members_tab_html = render_template(
        "partials/channel_details_tab_members.html",
        channel=channel,
        admins=admins,
        members=members,
        users_to_invite=users_to_invite,
        current_user_membership=current_user_membership,
    )

    # This is the new, correctly formatted OOB swap for the count badge
    members_count = len(admins) + len(members)
    count_swap_html = (
        f'<span id="channel-members-count-{channel_id}" hx-swap-oob="true" class="badge bg-secondary rounded-pill">'
        f"{members_count}"
        f"</span>"
    )

    return make_response(members_tab_html + count_swap_html)


@main_bp.route("/chat/channel/<int:channel_id>/members/search", methods=["GET"])
@login_required
def search_users_to_add(channel_id):
    """
    Searches for workspace members who are not in the current channel,
    with support for pagination.
    """
    search_term = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int)
    per_page = 15

    channel = Channel.get_by_id(channel_id)

    # Subquery to find all user IDs already in the channel
    members_subquery = ChannelMember.select(ChannelMember.user_id).where(
        ChannelMember.channel_id == channel_id
    )

    # Base query for users in the workspace but not in the channel
    query = (
        User.select()
        .join(WorkspaceMember)
        .where(
            (User.id.not_in(members_subquery))
            & (WorkspaceMember.workspace == channel.workspace)
        )
    )

    # Apply search filter if a query is provided
    if search_term:
        # Search against username OR display name
        query = query.where(
            (User.username.contains(search_term))
            | (User.display_name.contains(search_term))
        )

    total_users = query.count()
    users_for_page = query.order_by(User.username).paginate(page, per_page)
    has_more_pages = total_users > (page * per_page)

    return render_template(
        "partials/add_member_results.html",
        channel=channel,
        users_to_invite=users_for_page,
        has_more_pages=has_more_pages,
        current_page=page,
    )


@main_bp.route(
    "/chat/channel/<int:channel_id>/members/<int:user_id_to_remove>", methods=["DELETE"]
)
@login_required
def remove_channel_member(channel_id, user_id_to_remove):
    """Allows a channel admin to remove another member from the channel."""
    # ... (the first part of the function is correct and remains the same)
    channel = Channel.get_or_none(id=channel_id)
    user_to_remove = User.get_or_none(id=user_id_to_remove)
    if not channel or not user_to_remove:
        return "Channel or user not found", 404
    admin_membership = ChannelMember.get_or_none(user=g.user, channel=channel)
    if not admin_membership or admin_membership.role != "admin":
        return "You do not have permission to remove members.", 403
    if g.user.id == user_id_to_remove:
        return "You cannot remove yourself.", 400
    membership_to_delete = ChannelMember.get_or_none(
        user=user_to_remove, channel=channel
    )
    if membership_to_delete:
        if membership_to_delete.role == "admin":
            admin_count = (
                ChannelMember.select()
                .where(
                    (ChannelMember.channel == channel) & (ChannelMember.role == "admin")
                )
                .count()
            )
            if admin_count == 1:
                return "You cannot remove the last admin of the channel.", 403
        membership_to_delete.delete_instance()
        if user_id_to_remove in chat_manager.all_clients:
            try:
                remove_html = (
                    f'<div id="channel-item-{channel_id}" hx-swap-oob="delete"></div>'
                )
                notification = {
                    "type": "notification",
                    "title": "Removed from Channel",
                    "body": f"You have been removed from #{channel.name} by {g.user.username}.",
                    "icon": url_for("static", filename="favicon.ico", _external=True),
                }
                recipient_ws = chat_manager.all_clients[user_id_to_remove]
                recipient_ws.send(remove_html)
                recipient_ws.send(json.dumps(notification))
            except Exception as e:
                print(
                    f"Could not send removal notification to user {user_id_to_remove}: {e}"
                )

    # Re-render the members tab for the admin.
    admins = list(
        ChannelMember.select()
        .join(User)
        .where((ChannelMember.channel == channel) & (ChannelMember.role == "admin"))
        .order_by(User.username)
    )
    members = list(
        ChannelMember.select()
        .join(User)
        .where((ChannelMember.channel == channel) & (ChannelMember.role == "member"))
        .order_by(User.username)
    )
    subquery = ChannelMember.select(ChannelMember.user_id).where(
        ChannelMember.channel_id == channel_id
    )
    users_to_invite = (
        User.select()
        .join(WorkspaceMember)
        .where(User.id.not_in(subquery), WorkspaceMember.workspace == channel.workspace)
    )

    members_tab_html = render_template(
        "partials/channel_details_tab_members.html",
        channel=channel,
        admins=admins,
        members=members,
        users_to_invite=users_to_invite,
        current_user_membership=admin_membership,
    )

    # This is the new, correctly formatted OOB swap for the count badge
    members_count = len(admins) + len(members)
    count_swap_html = (
        f'<span id="channel-members-count-{channel_id}" hx-swap-oob="true" class="badge bg-secondary rounded-pill">'
        f"{members_count}"
        f"</span>"
    )

    return make_response(members_tab_html + count_swap_html)


# --- Route to get the channel creation form ---
@main_bp.route("/chat/channels/create", methods=["GET"])
@login_required
def get_create_channel_form():
    """Renders the HTMX partial for the channel creation form."""
    return render_template("partials/create_channel_form.html")


# --- Route to handle channel creation ---
@main_bp.route("/chat/channels/create", methods=["POST"])
@login_required
def create_channel():
    """Processes the new channel form submission."""
    channel_name = request.form.get("name", "").strip()
    is_private = request.form.get("is_private") == "on"

    channel_name = re.sub(r"[^a-zA-Z0-9_-]", "", channel_name).lower()

    if not channel_name or len(channel_name) < 3:
        error = "Name must be at least 3 characters long and contain only letters, numbers, underscores, or hyphens."
        return (
            render_template(
                "partials/create_channel_form.html",
                error=error,
                name=channel_name,
                is_private=is_private,
            ),
            400,
        )

    workspace_member = WorkspaceMember.get_or_none(user=g.user)
    if not workspace_member:
        return "You are not a member of any workspace.", 403
    workspace = workspace_member.workspace

    # Check if the "No channels found" placeholder is likely to be on the page
    # by counting the user's channels *before* we create the new one.
    user_channel_count = (
        ChannelMember.select().where(ChannelMember.user == g.user).count()
    )

    try:
        with db.atomic():
            new_channel = Channel.create(
                workspace=workspace,
                name=channel_name,
                is_private=is_private,
                created_by=g.user,
            )
            ChannelMember.create(user=g.user, channel=new_channel, role="admin")

            Conversation.get_or_create(
                conversation_id_str=f"channel_{new_channel.id}",
                defaults={"type": "channel"},
            )

    except IntegrityError:
        error = f"A channel named '#{channel_name}' already exists."
        return (
            render_template(
                "partials/create_channel_form.html",
                error=error,
                name=channel_name,
                is_private=is_private,
            ),
            409,
        )

    # 1. Render the OOB swap to add the new channel to the sidebar.
    new_sidebar_item_html = render_template(
        "partials/channel_list_item.html", channel=new_channel
    )
    # And the OOB swap to remove the "No channels found" placeholder.
    remove_placeholder_html = ""
    if user_channel_count == 0:
        remove_placeholder_html = (
            '<li id="no-channels-placeholder" hx-swap-oob="delete"></li>'
        )

    # 2. Prepare data for the main chat view swap.
    members_count = 1  # Just the creator
    messages = []  # It's a new channel

    # 3. Render the OOB swaps for the header and message containers.
    header_html = render_template(
        "partials/channel_header.html", channel=new_channel, members_count=members_count
    )
    messages_html = render_template(
        "partials/channel_messages.html",
        channel=new_channel,
        messages=messages,
        last_read_timestamp=datetime.datetime.now(),  # Mark as read
        mention_message_ids=set(),  # No mentions yet
        PAGE_SIZE=PAGE_SIZE,
    )

    # 4. Wrap the main content swaps in divs with OOB attributes.
    header_swap_html = (
        f'<div id="chat-header-container" hx-swap-oob="innerHTML">{header_html}</div>'
    )
    messages_swap_html = f'<div id="chat-messages-container" hx-swap-oob="innerHTML">{messages_html}</div>'

    # 5. Combine all HTML fragments into a single response payload.
    full_response_html = (
        new_sidebar_item_html
        + remove_placeholder_html
        + header_swap_html
        + messages_swap_html
    )

    # 6. Create the final response and add the trigger to close the modal.
    response = make_response(full_response_html)

    # Set focus in the chat input
    response.headers["HX-Trigger"] = "close-modal, focus-chat-input"

    # Update the screen
    return response


@main_bp.route("/chat/channel/<int:channel_id>/leave", methods=["POST"])
@login_required
def leave_channel(channel_id):
    """Allows the current user to leave a channel."""
    channel = Channel.get_or_none(id=channel_id)
    if not channel:
        response = make_response()
        response.headers["HX-Redirect"] = url_for("main.chat_interface")
        return response

    if channel.name == "announcements":
        return "You cannot leave the announcements channel.", 403

    membership = ChannelMember.get_or_none(user=g.user, channel=channel)
    if membership:
        # Check if the user is an admin
        if membership.role == "admin":
            # Count how many other members and admins are in the channel
            member_count = (
                ChannelMember.select().where(ChannelMember.channel == channel).count()
            )
            admin_count = (
                ChannelMember.select()
                .where(
                    (ChannelMember.channel == channel) & (ChannelMember.role == "admin")
                )
                .count()
            )

            # If this user is the last admin AND there are other members left, block them.
            if admin_count == 1 and member_count > 1:
                return (
                    "You must promote another member to admin before you can leave.",
                    403,
                )

        ChannelMember.delete().where(
            (ChannelMember.user == g.user) & (ChannelMember.channel == channel)
        ).execute()

    remove_from_list_html = (
        f'<div id="channel-item-{channel_id}" hx-swap-oob="delete"></div>'
    )

    user_self = g.user
    conv_id_str_self = f"dm_{user_self.id}_{user_self.id}"
    conversation_self, _ = Conversation.get_or_create(
        conversation_id_str=conv_id_str_self, defaults={"type": "dm"}
    )
    messages_self = (
        Message.select()
        .where(Message.conversation == conversation_self)
        .order_by(Message.created_at.desc())
        .limit(PAGE_SIZE)
    )

    dm_header_html = render_template("partials/dm_header.html", other_user=user_self)
    dm_messages_html = render_template(
        "partials/dm_messages.html",
        messages=list(reversed(messages_self)),
        other_user=user_self,
        last_read_timestamp=datetime.datetime.now(),
        PAGE_SIZE=PAGE_SIZE,
    )
    messages_swap_html = f'<div id="chat-messages-container" hx-swap-oob="innerHTML">{dm_messages_html}</div>'

    full_response_html = remove_from_list_html + dm_header_html + messages_swap_html
    response = make_response(full_response_html)
    response.headers["HX-Trigger"] = "close-offcanvas"

    return response


@main_bp.route(
    "/chat/channel/<int:channel_id>/members/<int:user_id_to_modify>/role",
    methods=["PUT"],
)
@login_required
def update_member_role(channel_id, user_id_to_modify):
    """Allows a channel admin to promote or demote another member."""
    new_role = request.form.get("role")
    channel = Channel.get_or_none(id=channel_id)
    user_to_modify = User.get_or_none(id=user_id_to_modify)

    if not all([channel, user_to_modify, new_role in ["admin", "member"]]):
        return "Invalid request parameters", 400

    admin_membership = ChannelMember.get_or_none(user=g.user, channel=channel)
    if not admin_membership or admin_membership.role != "admin":
        return "You do not have permission to change roles.", 403

    if g.user.id == user_id_to_modify:
        return "You cannot change your own role.", 400

    membership_to_modify = ChannelMember.get_or_none(
        user=user_to_modify, channel=channel
    )
    if membership_to_modify:
        if membership_to_modify.role == "admin" and new_role == "member":
            admin_count = (
                ChannelMember.select()
                .where(
                    (ChannelMember.channel == channel) & (ChannelMember.role == "admin")
                )
                .count()
            )
            if admin_count == 1:
                return "Cannot demote the last admin of the channel.", 403

        membership_to_modify.role = new_role
        membership_to_modify.save()

    admins = list(
        ChannelMember.select()
        .join(User)
        .where((ChannelMember.channel == channel) & (ChannelMember.role == "admin"))
        .order_by(User.username)
    )
    members = list(
        ChannelMember.select()
        .join(User)
        .where((ChannelMember.channel == channel) & (ChannelMember.role == "member"))
        .order_by(User.username)
    )

    subquery = ChannelMember.select(ChannelMember.user_id).where(
        ChannelMember.channel_id == channel_id
    )
    users_to_invite = (
        User.select()
        .join(WorkspaceMember)
        .where(User.id.not_in(subquery), WorkspaceMember.workspace == channel.workspace)
    )

    # Re-render the members tab to show the change.
    return render_template(
        "partials/channel_details_tab_members.html",
        channel=channel,
        admins=admins,
        members=members,
        users_to_invite=users_to_invite,
        current_user_membership=admin_membership,
    )


@main_bp.route("/chat/channel/<int:channel_id>/settings", methods=["PUT"])
@login_required
def update_channel_settings(channel_id):
    """Allows a channel admin to update channel-wide settings. Saves on toggle."""
    channel = Channel.get_or_none(id=channel_id)
    if not channel:
        return "Channel not found.", 404

    admin_membership = ChannelMember.get_or_none(user=g.user, channel=channel)
    if not admin_membership or admin_membership.role != "admin":
        return "You do not have permission to change settings.", 403

    # Get the value for the new toggle
    is_private_request = request.form.get("is_private") == "on"

    # Add a security check for special channels
    if channel.name == "announcements" and is_private_request:
        return "The announcements channel cannot be made private.", 403

    # Update the model with all three values
    channel.is_private = is_private_request
    channel.posting_restricted_to_admins = (
        request.form.get("posting_restricted") == "on"
    )
    channel.invites_restricted_to_admins = (
        request.form.get("invites_restricted") == "on"
    )
    channel.save()

    # On success, return an empty 200 OK. Our toast system will handle any errors.
    return "", 200


@main_bp.route("/chat/dms/start", methods=["GET"])
@login_required
def get_start_dm_form():
    """Gets the list of users a new DM can be started with."""
    # 1. Get the IDs of users the current user ALREADY has a DM with.
    dm_conversations = (
        Conversation.select()
        .join(UserConversationStatus)
        .where((UserConversationStatus.user == g.user) & (Conversation.type == "dm"))
    )

    existing_partner_ids = {g.user.id}  # Always exclude the user themselves
    for conv in dm_conversations:
        user_ids = [int(uid) for uid in conv.conversation_id_str.split("_")[1:]]
        partner_id = next((uid for uid in user_ids if uid != g.user.id), None)
        if partner_id:
            existing_partner_ids.add(partner_id)

    # 2. Select all users whose IDs are NOT in our exclusion list.
    users_to_start_dm = User.select().where(User.id.not_in(list(existing_partner_ids)))

    return render_template(
        "partials/start_dm_modal.html", users_to_start_dm=users_to_start_dm
    )


@main_bp.route("/chat/channels/browse", methods=["GET"])
@login_required
def get_browse_channels_modal():
    """
    Renders the modal for browsing channels, showing the first page of results.
    """
    page = 1
    per_page = 15

    # Subquery to find all channels the user is already a member of.
    member_of_channels_subquery = ChannelMember.select(ChannelMember.channel_id).where(
        ChannelMember.user == g.user
    )

    # Main query to find all channels that are public AND the user is not a member of.
    # We also join the User table to fetch the creator's username.
    query = (
        Channel.select(Channel, User)
        .join(
            User,
            join_type=JOIN.LEFT_OUTER,
            on=(Channel.created_by == User.id),
            attr="created_by",
        )
        .where(
            (Channel.is_private == False)
            & (Channel.id.not_in(member_of_channels_subquery))
        )
        .order_by(Channel.name)
    )

    # Get the total count before paginating
    total_channels = query.count()

    # Fetch the channels for the current page
    channels_for_page = query.paginate(page, per_page)

    # Determine if there's a next page
    has_more_pages = total_channels > (page * per_page)

    return render_template(
        "partials/browse_channels_modal.html",
        channels=channels_for_page,
        has_more_pages=has_more_pages,
        current_page=page,
    )


@main_bp.route("/chat/channels/search", methods=["GET"])
@login_required
def search_channels():
    """
    Searches for joinable public channels based on a query and supports pagination.
    Returns an HTML fragment with the filtered list of channels.
    """
    search_term = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int)
    per_page = 15

    # Subquery to find all channels the user is already a member of.
    member_of_channels_subquery = ChannelMember.select(ChannelMember.channel_id).where(
        ChannelMember.user == g.user
    )

    # Base query for channels, joining User to get creator info.
    query = (
        Channel.select(Channel, User)
        .join(
            User,
            join_type=JOIN.LEFT_OUTER,
            on=(Channel.created_by == User.id),
            attr="created_by",
        )
        .where(
            (Channel.is_private == False)
            & (Channel.id.not_in(member_of_channels_subquery))
        )
    )

    # If a search term is provided, filter the query by name.
    if search_term:
        query = query.where(Channel.name.contains(search_term))

    # Get the total count of matching channels before paginating
    total_channels = query.count()

    # Fetch the specific page of channels
    channels_for_page = query.order_by(Channel.name).paginate(page, per_page)

    # Determine if there's a next page
    has_more_pages = total_channels > (page * per_page)

    # Render *only* the results partial.
    return render_template(
        "partials/joinable_channel_results.html",
        channels=channels_for_page,
        has_more_pages=has_more_pages,
        current_page=page,
    )


@main_bp.route("/chat/channel/<int:channel_id>/join", methods=["POST"])
@login_required
def join_channel(channel_id):
    """Adds the current user to a public channel."""
    channel = Channel.get_or_none(id=channel_id)
    if not channel:
        return "Channel not found.", 404

    if channel.is_private:
        return "You cannot join a private channel.", 403

    is_already_member = (
        ChannelMember.select()
        .where((ChannelMember.user == g.user) & (ChannelMember.channel == channel))
        .exists()
    )

    if not is_already_member:
        ChannelMember.create(user=g.user, channel=channel)

    # OOB swap to add the new channel to the user's sidebar list
    new_sidebar_item_html = render_template(
        "partials/channel_list_item.html", channel=channel
    )

    # Re-query the channel with the creator info to render the confirmation message
    channel_with_creator = (
        Channel.select(Channel, User)
        .join(
            User,
            join_type=JOIN.LEFT_OUTER,
            on=(Channel.created_by == User.id),
            attr="created_by",
        )
        .where(Channel.id == channel_id)
        .get()
    )

    # This is the main response, which renders our new partial for the joined state.
    confirmation_html = render_template(
        "partials/joined_channel_item.html", channel=channel_with_creator
    )

    return new_sidebar_item_html + confirmation_html


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
    Deletes a message.
    """
    message = Message.get_or_none(id=message_id)
    if not message or message.user.id != g.user.id:
        return "Unauthorized", 403

    # Get the conversation ID before deleting the message object
    conv_id_str = message.conversation.conversation_id_str

    # Delete the message from the database
    message.delete_instance()

    # Construct the OOB swap HTML to delete the element on all clients' screens
    broadcast_html = f'<div id="message-{message_id}" hx-swap-oob="delete"></div>'

    # Broadcast the delete instruction
    chat_manager.broadcast(conv_id_str, broadcast_html)

    # The hx-delete request expects an empty response since the target is removed
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
    return render_template(
        "partials/chat_input_reply.html", message=message_to_reply_to
    )


# --- PROFILE EDITING ROUTES ---
@main_bp.route("/profile/address/view", methods=["GET"])
@login_required
def get_address_display():
    """Returns the read-only address display partial."""
    return render_template("partials/address_display.html", user=g.user)


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

    # IMPORTANT: Update the header and then return the display partial
    header_html = render_template("partials/profile_header_oob.html", user=user)
    display_html = render_template("partials/address_display.html", user=user)

    return make_response(header_html + display_html)


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


@main_bp.route("/chat/dm/<int:other_user_id>")
@login_required
def get_dm_chat(other_user_id):
    other_user = User.get_or_none(id=other_user_id)
    if not other_user:
        return "User not found", 404

    user_ids = sorted([g.user.id, other_user.id])
    conv_id_str = f"dm_{user_ids[0]}_{user_ids[1]}"
    conversation, _ = Conversation.get_or_create(
        conversation_id_str=conv_id_str, defaults={"type": "dm"}
    )

    # [THE FIX] Use the 'created' flag from get_or_create to reliably know if this is a new DM for the user
    status, created = UserConversationStatus.get_or_create(
        user=g.user, conversation=conversation
    )
    last_read_timestamp = status.last_read_timestamp
    status.last_read_timestamp = datetime.datetime.now()
    status.save()
    UserConversationStatus.get_or_create(user=other_user, conversation=conversation)

    messages = list(
        Message.select()
        .where(Message.conversation == conversation)
        .order_by(Message.created_at.desc())
        .limit(PAGE_SIZE)
    )
    messages.reverse()
    reactions_map = get_reactions_for_messages(messages)

    header_html_content = render_template(
        "partials/dm_header.html", other_user=other_user
    )
    header_html = f'<div id="chat-header-container" hx-swap-oob="true">{header_html_content}</div>'

    # This is the main content
    messages_html = render_template(
        "partials/dm_messages.html",
        messages=messages,
        other_user=other_user,
        last_read_timestamp=last_read_timestamp,
        PAGE_SIZE=PAGE_SIZE,
        reactions_map=reactions_map,
    )

    clear_badge_html = ""
    add_to_sidebar_html = ""
    # If the DM already existed for this user, send command to clear the badge.
    if not created and other_user.id != g.user.id:
        clear_badge_html = render_template(
            "partials/clear_badge.html",
            conv_id_str=conv_id_str,
            hx_get_url=url_for("main.get_dm_chat", other_user_id=other_user.id),
            link_text=other_user.display_name or other_user.username,
        )
    # If this is the first time this user is opening this DM, send command to add it to the sidebar.
    elif created and other_user.id != g.user.id:
        add_to_sidebar_html = render_template(
            "partials/dm_list_item_oob.html",
            user=other_user,
            conv_id_str=conv_id_str,
            is_online=other_user.id in chat_manager.online_users,
        )

    full_response = messages_html + header_html + clear_badge_html + add_to_sidebar_html
    response = make_response(full_response)
    response.headers["HX-Trigger"] = "load-chat-history"
    return response


@main_bp.route("/profile/status", methods=["PUT"])
@login_required
def update_presence_status():
    """Updates the user's presence status and broadcasts the change."""
    new_status = request.form.get("status")
    if new_status and new_status in STATUS_CLASS_MAP:
        user = g.user
        user.presence_status = new_status
        user.save()

        # --- Broadcast the changes to all connected clients ---

        # 1. Broadcast the update for the DM list dots (uses bg-* classes)
        status_class = STATUS_CLASS_MAP.get(new_status, "bg-secondary")
        dm_list_presence_html = f'<span id="status-dot-{user.id}" class="me-2 rounded-circle {status_class}" style="width: 10px; height: 10px;" hx-swap-oob="true"></span>'
        chat_manager.broadcast_to_all(dm_list_presence_html)

        # 2. [THE FIX] Broadcast a SECOND, separate update for the sidebar profile button
        #    This uses the custom presence-* classes.
        profile_status_map = {
            "online": "presence-online",
            "away": "presence-away",
            "busy": "presence-busy",
        }
        profile_status_class = profile_status_map.get(new_status, "presence-away")
        sidebar_presence_html = f'<span id="sidebar-presence-indicator-{user.id}" class="presence-indicator {profile_status_class}" hx-swap-oob="true"></span>'
        chat_manager.broadcast_to_all(sidebar_presence_html)

        # 3. Also update the indicator on the profile page itself (if other tabs are open)
        profile_page_presence_html = f'<span id="profile-presence-indicator-{user.id}" class="presence-indicator {profile_status_class}" hx-swap-oob="true"></span>'
        chat_manager.broadcast_to_all(profile_page_presence_html)

        # Return the updated profile header to the user who made the change
        return render_template("partials/profile_header.html", user=user)

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
            hx_get_url=url_for("main.get_channel_chat", channel_id=channel.id),
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
                hx_get_url=url_for("main.get_dm_chat", other_user_id=other_user.id),
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

    # When a user connects, broadcast their ACTUAL saved status, not just "online".
    status_class = STATUS_CLASS_MAP.get(user.presence_status, "bg-secondary")
    presence_html = f'<span id="status-dot-{user.id}" class="me-2 rounded-circle {status_class}" style="width: 10px; height: 10px;" hx-swap-oob="true"></span>'
    chat_manager.broadcast_to_all(presence_html)

    try:
        while True:
            data = json.loads(ws.receive())
            event_type = data.get("type")

            if event_type in ["typing_start", "typing_stop"]:
                is_typing = event_type == "typing_start"
                indicator_html = f'<div id="typing-indicator" hx-swap-oob="true">{f"<p>{ws.user.username} is typing...</p>" if is_typing else ""}</div>'
                chat_manager.broadcast(
                    data.get("conversation_id"), indicator_html, sender_ws=ws
                )
                continue

            if event_type == "subscribe":
                conv_id_str = data.get("conversation_id")
                if conv_id_str:
                    chat_manager.subscribe(conv_id_str, ws)
                continue

            chat_text = data.get("chat_message")
            parent_id = data.get("parent_message_id")
            conv_id_str = getattr(ws, "channel_id", None)

            if not (chat_text and conv_id_str):
                continue

            conversation = Conversation.get_or_none(conversation_id_str=conv_id_str)
            if not conversation:
                continue

            # Check posting permissions
            if conversation.type == "channel":
                channel = Channel.get_by_id(
                    conversation.conversation_id_str.split("_")[1]
                )
                if channel.posting_restricted_to_admins:
                    membership = ChannelMember.get_or_none(
                        user=ws.user, channel=channel
                    )
                    if not membership or membership.role != "admin":
                        # Silently ignore the message if the user doesn't have permission
                        continue

            with db.atomic():
                new_message = Message.create(
                    user=ws.user,
                    conversation=conversation,
                    content=chat_text,
                    parent_message=parent_id if parent_id else None,
                )
                mentioned_usernames = set(re.findall(r"@(\w+)", chat_text))
                if mentioned_usernames:
                    mentioned_users = User.select().where(
                        User.username.in_(list(mentioned_usernames))
                    )
                    for mentioned_user in mentioned_users:
                        Mention.get_or_create(user=mentioned_user, message=new_message)

            if (
                conversation.type == "dm"
                and Message.select().where(Message.conversation == conversation).count()
                == 1
            ):
                user_ids = [int(uid) for uid in conv_id_str.split("_")[1:]]
                recipient_id = next(
                    (uid for uid in user_ids if uid != ws.user.id), None
                )
                if recipient_id and recipient_id in chat_manager.all_clients:
                    add_sender_html = render_template(
                        "partials/dm_list_item_oob.html",
                        user=ws.user,
                        conv_id_str=conv_id_str,
                        is_online=True,
                    )
                    chat_manager.all_clients[recipient_id].send(add_sender_html)

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

            # 1. Render the new message partial once.
            new_message_html = render_template(
                "partials/message.html", message=new_message
            )

            # 2. This is the OOB fragment to append the message to the viewer's list.
            message_to_broadcast = (
                f'<div hx-swap-oob="beforeend:#message-list">{new_message_html}</div>'
            )

            # 3. Broadcast the message ONLY to other clients viewing the channel.
            #    This prevents the oobErrorNoTarget by not sending to users who don't have #message-list rendered.
            chat_manager.broadcast(conv_id_str, message_to_broadcast, sender_ws=ws)

            # 4. The original sender also needs to receive the message.
            #    We also check if they were replying, and if so, tack on the command to reset their input field.
            message_for_sender = message_to_broadcast
            if parent_id:
                input_html = render_template("partials/chat_input_default.html")
                message_for_sender += f'<div id="chat-input-container" hx-swap-oob="outerHTML">{input_html}</div>'
            ws.send(message_for_sender)

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
                # Don't notify the person who sent the message or users who aren't connected
                if member.id == ws.user.id or member.id not in chat_manager.all_clients:
                    continue

                member_ws = chat_manager.all_clients[member.id]

                # If the user is actively viewing this conversation, do nothing.
                if getattr(member_ws, "channel_id", None) == conv_id_str:
                    continue

                # This user is eligible for notifications. Get their status record.
                status, _ = UserConversationStatus.get_or_create(
                    user=member, conversation=conversation
                )

                # 1. Handle UI badge/bolding updates (this part is mostly the same)
                notification_html = None
                if conversation.type == "channel":
                    channel_model = Channel.get_by_id(
                        conversation.conversation_id_str.split("_")[1]
                    )
                    link_text = f"# {channel_model.name}"
                    hx_get_url = url_for(
                        "main.get_channel_chat", channel_id=channel_model.id
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
                        total_mentions = (
                            Mention.select()
                            .join(Message)
                            .where(
                                (Mention.user == member)
                                & (Message.conversation == conversation)
                            )
                            .count()
                        )
                        notification_html = render_template(
                            "partials/unread_badge.html",
                            conv_id_str=conv_id_str,
                            count=total_mentions,
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
                    hx_get_url = url_for("main.get_dm_chat", other_user_id=ws.user.id)
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

                # 2. Handle Desktop Notification with Cooldown
                now = datetime.datetime.now()
                send_notification = False
                if status.last_notified_timestamp is None or (
                    now - status.last_notified_timestamp
                ) > datetime.timedelta(seconds=60):
                    send_notification = True

                if send_notification:
                    notification_payload = {
                        "type": "notification",
                        "title": f"New message from {new_message.user.display_name or new_message.user.username}",
                        "body": new_message.content,
                        "icon": url_for(
                            "static", filename="favicon.ico", _external=True
                        ),
                        "tag": conv_id_str,
                    }
                    member_ws.send(json.dumps(notification_payload))
                    # Update the timestamp in the database
                    status.last_notified_timestamp = now
                    status.save()
                else:
                    # 3. If notification is on cooldown, just send a sound trigger
                    sound_payload = {"type": "sound"}
                    member_ws.send(json.dumps(sound_payload))

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
