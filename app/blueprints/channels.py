# app/blueprints/channels.py
from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    g,
    make_response,
)
from app.models import (
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
from app.routes import (
    login_required,
    PAGE_SIZE,
    get_reactions_for_messages,
    get_attachments_for_messages,
    check_and_get_read_state_oob,
)
from app.chat_manager import chat_manager
import json
from peewee import IntegrityError, JOIN
import re
import datetime

channels_bp = Blueprint("channels", __name__)


@channels_bp.route("/chat/channel/<int:channel_id>")
@login_required
def get_channel_chat(channel_id):
    channel = Channel.get_or_none(id=channel_id)
    if not channel:
        return "Channel not found", 404

    # Are the a member of this channel?
    is_member = (
        ChannelMember.select()
        .where((ChannelMember.user == g.user) & (ChannelMember.channel == channel))
        .exists()
    )

    if channel.is_private and not is_member:
        return "You are not a member of this private channel.", 403

    add_to_sidebar_html = ""
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
    last_seen_mention = status.last_seen_mention_id or 0
    status.last_read_timestamp = datetime.datetime.now()
    status.save()

    # Get the latest messages
    messages = list(
        Message.select()
        .where(Message.conversation == conversation)
        .order_by(Message.created_at.desc())
        .limit(PAGE_SIZE)
    )
    messages.reverse()
    reactions_map = get_reactions_for_messages(messages)
    attachments_map = get_attachments_for_messages(messages)
    members_count = (
        ChannelMember.select().where(ChannelMember.channel == channel).count()
    )

    # Process mentions
    mention_messages_query = (
        Message.select(Message.id)
        .join(Mention)
        .where(
            (Message.conversation == conversation)
            & (Mention.user == g.user)
            & (Message.id > last_seen_mention)  # Only get mentions with a higher ID
        )
    )
    # Convert the query to a set of IDs for lookup in the template
    mention_message_ids = {m.id for m in mention_messages_query}

    header_html_content = render_template(
        "partials/channel_header.html", channel=channel, members_count=members_count
    )
    header_html = f'<div id="chat-header-container" hx-swap-oob="true">{header_html_content}</div>'

    messages_html = render_template(
        "partials/channel_messages.html",
        channel=channel,
        messages=messages,
        last_read_timestamp=last_read_timestamp,
        mention_message_ids=mention_message_ids,
        PAGE_SIZE=PAGE_SIZE,
        reactions_map=reactions_map,
        attachments_map=attachments_map,
        conversation_id=conversation.id,
        Message=Message,
    )

    clear_badge_html = render_template(
        "partials/clear_badge.html",
        conv_id_str=conv_id_str,
        hx_get_url=url_for("channels.get_channel_chat", channel_id=channel.id),
        link_text=f"# {channel.name}",
    )

    # Also render the default chat input to ensure it's present.
    chat_input_html = render_template("partials/chat_input_default.html")
    # Wrap it in a container with the correct ID for the OOB swap.
    chat_input_oob_html = f'<div id="chat-input-container" hx-swap-oob="outerHTML">{chat_input_html}</div>'

    # Check for other unreads and add the result to the response
    read_state_oob_html = check_and_get_read_state_oob(g.user, conversation)

    full_response = (
        messages_html
        + header_html
        + clear_badge_html
        + add_to_sidebar_html
        + chat_input_oob_html
        + read_state_oob_html
    )
    response = make_response(full_response)
    response.headers["HX-Trigger"] = "load-chat-history"
    return response


@channels_bp.route(
    "/chat/conversation/<int:conversation_id>/seen_mentions", methods=["POST"]
)
@login_required
def update_seen_mentions(conversation_id):
    """
    Updates the user's status to indicate they have seen mentions up to a certain message ID.
    """
    last_message_id = request.form.get("last_message_id", type=int)
    if not last_message_id:
        return "Invalid request", 400

    (
        UserConversationStatus.update(last_seen_mention_id=last_message_id)
        .where(
            (UserConversationStatus.user == g.user)
            & (UserConversationStatus.conversation == conversation_id)
        )
        .execute()
    )

    return "", 204  # Return "No Content" on success


@channels_bp.route("/chat/channel/<int:channel_id>/details", methods=["GET"])
@login_required
def get_channel_details(channel_id):
    """Renders the channel details shell with the default 'About' tab."""
    channel = Channel.get_or_none(id=channel_id)
    if not channel:
        return "Channel not found", 404

    current_user_membership = ChannelMember.get_or_none(user=g.user, channel=channel)
    if not current_user_membership:
        return "You are not a member of this channel.", 403

    admins = list(
        ChannelMember.select().where(
            (ChannelMember.channel == channel) & (ChannelMember.role == "admin")
        )
    )
    members_count = (
        ChannelMember.select().where(ChannelMember.channel == channel).count()
    )

    response = make_response(
        render_template(
            "partials/channel_details.html",
            channel=channel,
            admins=admins,
            members_count=members_count,
            current_user_membership=current_user_membership,
        )
    )
    response.headers["HX-Trigger"] = "open-offcanvas"
    return response


@channels_bp.route("/chat/channel/<int:channel_id>/details/about", methods=["GET"])
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


@channels_bp.route("/chat/channel/<int:channel_id>/details/members", methods=["GET"])
@login_required
def get_channel_details_members_tab(channel_id):
    """
    Renders the content for the 'Members' tab, showing current members.
    """
    channel = Channel.get_by_id(channel_id)
    current_user_membership = ChannelMember.get_or_none(user=g.user, channel=channel)

    if not current_user_membership:
        return "Unauthorized", 403

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

    return render_template(
        "partials/channel_details_tab_members.html",
        channel=channel,
        admins=admins,
        members=members,
        current_user_membership=current_user_membership,
    )


@channels_bp.route("/chat/channel/<int:channel_id>/details/settings", methods=["GET"])
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


@channels_bp.route("/chat/channel/<int:channel_id>/about", methods=["GET"])
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


@channels_bp.route("/chat/channel/<int:channel_id>/about/edit", methods=["GET"])
@login_required
def get_channel_about_form(channel_id):
    """Returns the form for editing channel details."""
    channel = Channel.get_by_id(channel_id)
    membership = ChannelMember.get_or_none(user=g.user, channel=channel)
    if not membership or membership.role != "admin":
        return "Unauthorized", 403

    return render_template("partials/channel_details_about_form.html", channel=channel)


@channels_bp.route("/chat/channel/<int:channel_id>/about", methods=["PUT"])
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

    current_user_membership = ChannelMember.get_or_none(user=g.user, channel=channel)
    display_html = render_template(
        "partials/channel_details_about_display.html",
        channel=channel,
        current_user_membership=current_user_membership,
    )

    members_count = (
        ChannelMember.select().where(ChannelMember.channel == channel).count()
    )
    header_html = render_template(
        "partials/channel_header.html", channel=channel, members_count=members_count
    )

    chat_manager.broadcast(f"channel_{channel.id}", header_html)

    return display_html


@channels_bp.route("/chat/channel/<int:channel_id>/members", methods=["POST"])
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
    conversation, _ = Conversation.get_or_create(
        conversation_id_str=f"channel_{channel_id}", defaults={"type": "channel"}
    )

    UserConversationStatus.get_or_create(
        user_id=user_id_to_add, conversation=conversation
    )
    if user_id_to_add in chat_manager.all_clients:
        try:
            recipient_ws = chat_manager.all_clients[user_id_to_add]
            new_channel_html = render_template(
                "partials/channel_list_item.html", channel=channel
            )
            recipient_ws.send(new_channel_html)
        except Exception as e:
            print(f"Could not send real-time channel add to user {user_id_to_add}: {e}")

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

    members_tab_html = render_template(
        "partials/channel_details_tab_members.html",
        channel=channel,
        admins=admins,
        members=members,
        current_user_membership=current_user_membership,
    )

    members_count = len(admins) + len(members)
    count_swap_html = (
        f'<span id="channel-members-count-{channel_id}" hx-swap-oob="true" class="badge bg-secondary rounded-pill">'
        f"{members_count}"
        f"</span>"
    )

    return make_response(members_tab_html + count_swap_html)


@channels_bp.route("/chat/channel/<int:channel_id>/members/search", methods=["GET"])
@login_required
def search_users_to_add(channel_id):
    """
    Searches for workspace members who are not in the current channel.
    """
    search_term = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int)
    per_page = 15

    channel = Channel.get_by_id(channel_id)

    members_subquery = ChannelMember.select(ChannelMember.user_id).where(
        ChannelMember.channel_id == channel_id
    )

    query = (
        User.select()
        .join(WorkspaceMember)
        .where(
            (User.id.not_in(members_subquery))
            & (WorkspaceMember.workspace == channel.workspace)
        )
    )

    if search_term:
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


@channels_bp.route(
    "/chat/channel/<int:channel_id>/members/<int:user_id_to_remove>", methods=["DELETE"]
)
@login_required
def remove_channel_member(channel_id, user_id_to_remove):
    """Allows a channel admin to remove another member from the channel."""
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

    members_tab_html = render_template(
        "partials/channel_details_tab_members.html",
        channel=channel,
        admins=admins,
        members=members,
        current_user_membership=admin_membership,
    )

    members_count = len(admins) + len(members)
    count_swap_html = (
        f'<span id="channel-members-count-{channel_id}" hx-swap-oob="true" class="badge bg-secondary rounded-pill">'
        f"{members_count}"
        f"</span>"
    )

    return make_response(members_tab_html + count_swap_html)


@channels_bp.route("/chat/channels/create", methods=["GET"])
@login_required
def get_create_channel_form():
    """Renders the HTMX partial for the channel creation form."""
    return render_template("partials/create_channel_form.html")


@channels_bp.route("/chat/channels/create", methods=["POST"])
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

    new_sidebar_item_html = render_template(
        "partials/channel_list_item.html", channel=new_channel
    )
    remove_placeholder_html = ""
    if user_channel_count == 0:
        remove_placeholder_html = (
            '<li id="no-channels-placeholder" hx-swap-oob="delete"></li>'
        )

    members_count = 1
    messages = []

    header_html = render_template(
        "partials/channel_header.html", channel=new_channel, members_count=members_count
    )
    messages_html = render_template(
        "partials/channel_messages.html",
        channel=new_channel,
        messages=messages,
        last_read_timestamp=datetime.datetime.now(),
        mention_message_ids=set(),
        PAGE_SIZE=PAGE_SIZE,
    )

    header_swap_html = (
        f'<div id="chat-header-container" hx-swap-oob="innerHTML">{header_html}</div>'
    )
    messages_swap_html = f'<div id="chat-messages-container" hx-swap-oob="innerHTML">{messages_html}</div>'

    full_response_html = (
        new_sidebar_item_html
        + remove_placeholder_html
        + header_swap_html
        + messages_swap_html
    )

    response = make_response(full_response_html)
    response.headers["HX-Trigger"] = "close-modal, focus-chat-input"
    return response


@channels_bp.route("/chat/channel/<int:channel_id>/leave", methods=["POST"])
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
        if membership.role == "admin":
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
    messages_query = (
        Message.select()
        .where(Message.conversation == conversation_self)
        .order_by(Message.created_at.desc())
        .limit(PAGE_SIZE)
    )

    # We need to fetch reactions and attachments for the messages we are about to render.
    messages_self = list(reversed(messages_query))
    reactions_map = get_reactions_for_messages(messages_self)
    attachments_map = get_attachments_for_messages(messages_self)

    dm_header_html = render_template("partials/dm_header.html", other_user=user_self)
    # Pass the new maps to the template context.
    dm_messages_html = render_template(
        "partials/dm_messages.html",
        messages=messages_self,
        other_user=user_self,
        last_read_timestamp=datetime.datetime.now(),
        PAGE_SIZE=PAGE_SIZE,
        reactions_map=reactions_map,
        attachments_map=attachments_map,
        Message=Message,
    )
    messages_swap_html = f'<div id="chat-messages-container" hx-swap-oob="innerHTML">{dm_messages_html}</div>'

    full_response_html = remove_from_list_html + dm_header_html + messages_swap_html
    response = make_response(full_response_html)
    response.headers["HX-Trigger"] = "close-offcanvas"

    return response


@channels_bp.route(
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

    return render_template(
        "partials/channel_details_tab_members.html",
        channel=channel,
        admins=admins,
        members=members,
        current_user_membership=admin_membership,
    )


@channels_bp.route("/chat/channel/<int:channel_id>/settings", methods=["PUT"])
@login_required
def update_channel_settings(channel_id):
    """Allows a channel admin to update channel-wide settings."""
    channel = Channel.get_or_none(id=channel_id)
    if not channel:
        return "Channel not found.", 404

    admin_membership = ChannelMember.get_or_none(user=g.user, channel=channel)
    if not admin_membership or admin_membership.role != "admin":
        return "You do not have permission to change settings.", 403

    is_private_request = request.form.get("is_private") == "on"

    if channel.name == "announcements" and is_private_request:
        return "The announcements channel cannot be made private.", 403

    channel.is_private = is_private_request
    channel.posting_restricted_to_admins = (
        request.form.get("posting_restricted") == "on"
    )
    channel.invites_restricted_to_admins = (
        request.form.get("invites_restricted") == "on"
    )
    channel.save()

    return "", 200


@channels_bp.route("/chat/channels/browse", methods=["GET"])
@login_required
def get_browse_channels_modal():
    """
    Renders the modal for browsing channels.
    """
    page = 1
    per_page = 15

    member_of_channels_subquery = ChannelMember.select(ChannelMember.channel_id).where(
        ChannelMember.user == g.user
    )

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

    total_channels = query.count()
    channels_for_page = query.paginate(page, per_page)
    has_more_pages = total_channels > (page * per_page)

    return render_template(
        "partials/browse_channels_modal.html",
        channels=channels_for_page,
        has_more_pages=has_more_pages,
        current_page=page,
    )


@channels_bp.route("/chat/channels/search", methods=["GET"])
@login_required
def search_channels():
    """
    Searches for joinable public channels.
    """
    search_term = request.args.get("q", "").strip()
    page = request.args.get("page", 1, type=int)
    per_page = 15

    member_of_channels_subquery = ChannelMember.select(ChannelMember.channel_id).where(
        ChannelMember.user == g.user
    )

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

    if search_term:
        query = query.where(Channel.name.contains(search_term))

    total_channels = query.count()
    channels_for_page = query.order_by(Channel.name).paginate(page, per_page)
    has_more_pages = total_channels > (page * per_page)

    return render_template(
        "partials/joinable_channel_results.html",
        channels=channels_for_page,
        has_more_pages=has_more_pages,
        current_page=page,
    )


@channels_bp.route("/chat/channel/<int:channel_id>/join", methods=["POST"])
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

    new_sidebar_item_html = render_template(
        "partials/channel_list_item.html", channel=channel
    )

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

    confirmation_html = render_template(
        "partials/joined_channel_item.html", channel=channel_with_creator
    )

    return new_sidebar_item_html + confirmation_html


@channels_bp.route("/chat/conversation/<conversation_id_str>/mention_search")
@login_required
def mention_search(conversation_id_str):
    """
    Searches for users and special keywords (@here, @channel) within a
    given conversation to populate the @mention popover.
    """

    # local vars
    members = []
    special_mentions = []

    query = request.args.get("q", "").lower()
    conversation = Conversation.get_or_none(conversation_id_str=conversation_id_str)

    if not conversation:
        return "", 404

    if conversation.type == "channel":
        channel = Channel.get_by_id(conversation.conversation_id_str.split("_")[1])
        channel_member_count = (
            ChannelMember.select().where(ChannelMember.channel == channel).count()
        )
        member_ids_query = ChannelMember.select(ChannelMember.user_id).where(
            ChannelMember.channel == channel
        )
        member_ids = {m.user_id for m in member_ids_query}
        online_ids = member_ids.intersection(chat_manager.online_users.keys())
        online_member_count = len(online_ids)

        # Helper for pluralizing "member" vs "members"
        channel_plural = "member" if channel_member_count == 1 else "members"
        online_plural = "member" if online_member_count == 1 else "members"
        # --- End of calculation block ---

        if not query or "here".startswith(query):
            special_mentions.append(
                {
                    "username": "here",
                    "display_name": "@here",
                    "description": f"Notifies {online_member_count} online {online_plural}.",
                }
            )
        if not query or "channel".startswith(query):
            special_mentions.append(
                {
                    "username": "channel",
                    "display_name": "@channel",
                    "description": f"Notifies all {channel_member_count} {channel_plural}.",
                }
            )

        members_query = (
            User.select()
            .join(ChannelMember)
            .where(
                (ChannelMember.channel == channel)
                & (
                    (User.username.startswith(query))
                    | (User.display_name.ilike(f"{query}%"))
                )
            )
            .limit(10)
        )
        members = list(members_query)

    elif conversation.type == "dm":
        user_ids = [int(uid) for uid in conversation.conversation_id_str.split("_")[1:]]
        members_query = User.select().where(
            (User.id.in_(user_ids))
            & (
                (User.username.startswith(query))
                | (User.display_name.ilike(f"{query}%"))
            )
        )
        members = list(members_query)

    return render_template(
        "partials/mention_results.html",
        users=members,
        special_mentions=special_mentions,
    )
