from flask import Blueprint, render_template, request, redirect, url_for, session, g, make_response
from .models import User, Channel, ChannelMember, Message, Conversation, Workspace, WorkspaceMember, db, UserConversationStatus, Mention
from .sso import oauth # Import the oauth object
import functools
import secrets
from . import sock
from .chat_manager import chat_manager
import json
from peewee import IntegrityError, fn
import re
import datetime
from functools import reduce
import operator

# Main blueprint for general app routes
main_bp = Blueprint('main', __name__)

# Admin blueprint for admin-specific routes
admin_bp = Blueprint('admin', __name__)

# A central map for presence status to Bootstrap CSS classes.
STATUS_CLASS_MAP = {
    'online': 'bg-success',
    'away': 'bg-secondary',
    'busy': 'bg-warning'  # Bootstrap's yellow
}

# This function runs before every request to load the logged-in user
@main_bp.before_app_request
def load_logged_in_user():
    user_id = session.get('user_id')
    if user_id is None:
        g.user = None
    else:
        g.user = User.get_or_none(User.id == user_id)

# Decorator to require login for a route
def login_required(view):
    @functools.wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            return redirect(url_for('main.login_page'))
        return view(**kwargs)
    return wrapped_view

# --- Helpers ---
def to_html(text):
    """
    Converts markdown text to HTML, using the same extensions as the
    Jinja filter for consistency.
    """
    return markdown.markdown(text, extensions=[
        'fenced_code', 'codehilite', 'sane_lists', 'nl2br'
    ])

# --- Routes ---
@main_bp.route('/')
def index():
    return render_template('index.html')

@main_bp.route('/login')
def login_page():
    return render_template('login.html')

@main_bp.route('/sso-login')
def sso_login():
    """Redirects to the SSO provider for login."""

    redirect_uri = url_for('main.authorize', _external=True)

    # Generate a cryptographically secure nonce
    nonce = secrets.token_urlsafe(16)
    # Store the nonce in the session for later verification
    session['nonce'] = nonce

    return oauth.authentik.authorize_redirect(redirect_uri, nonce=nonce)

@main_bp.route('/auth')
def authorize():
    """The callback route for the SSO provider."""
    # The actual logic is in app/sso.py, but we need the route here
    from .sso import handle_auth_callback
    return handle_auth_callback()

@main_bp.route('/logout')
def logout():
    """Logs the user out by clearing the session."""
    session.clear()
    return redirect(url_for('main.index'))

# A simple profile page to show after login
@main_bp.route('/profile')
@login_required
def profile():
    return render_template('profile.html', user=g.user, theme=g.user.theme)


# --- CHAT INTERFACE ROUTES ---
@main_bp.route('/chat')
@login_required
def chat_interface():
    """Renders the main chat UI."""

    # Local Vars
    unread_counts = {}
    dm_partner_ids = set()

    # Fetch channels the current user is a member of
    user_channels = (Channel.select(Channel, Conversation)
                    .join(ChannelMember).where(ChannelMember.user == g.user)
                    .join_from(Channel, Conversation,
                     on=(Conversation.conversation_id_str == fn.CONCAT('channel_', Channel.id))))

    # 1. Find all DM conversations the current user is a part of.
    dm_conversations = (Conversation.select()
                        .join(UserConversationStatus)
                        .where((UserConversationStatus.user == g.user) & (Conversation.type == 'dm')))

    # 2. From those conversations, extract the IDs of the *other* users.
    for conv in dm_conversations:
        user_ids = [int(uid) for uid in conv.conversation_id_str.split('_')[1:]]
        if len(user_ids) > 1:
            partner_id = next((uid for uid in user_ids if uid != g.user.id), None)
            if partner_id:
                dm_partner_ids.add(partner_id)

    # 3. Fetch the User objects for the sidebar.
    direct_message_users = User.select().where(User.id.in_(list(dm_partner_ids)))

    # --- Calculate Unread Counts ---
    unread_counts = {}
    user_statuses = (UserConversationStatus.select()
                     .where(UserConversationStatus.user == g.user))
    last_read_map = {status.conversation.id: status.last_read_timestamp for status in user_statuses}

    user_conv_ids = set(last_read_map.keys())
    for channel in user_channels:
        if channel.conversation:
            user_conv_ids.add(channel.conversation.id)

    all_user_convs = Conversation.select().where(Conversation.id.in_(list(user_conv_ids)))
    conv_id_to_str_map = {conv.id: conv.conversation_id_str for conv in all_user_convs}

    # 5. Count unread messages for each conversation.
    for conv_id, conv_id_str in conv_id_to_str_map.items():
        last_read_time = last_read_map.get(conv_id, datetime.datetime.min)
        count = (Message.select()
                 .where(
                     (Message.conversation_id == conv_id) &
                     (Message.created_at > last_read_time) &
                     (Message.user != g.user)
                 ).count())
        unread_counts[conv_id_str] = count

    return render_template('chat.html',
                       channels=user_channels,
                       direct_message_users=direct_message_users,
                       online_users=chat_manager.online_users,
                       unread_counts=unread_counts,
                       theme=g.user.theme)


@main_bp.route('/chat/channel/<int:channel_id>')
@login_required
def get_channel_chat(channel_id):
    channel = (Channel.select()
               .join(ChannelMember)
               .where(Channel.id == channel_id, ChannelMember.user == g.user)
               .get_or_none())

    if not channel: return "Not a member of this channel", 403

    # Find or create the corresponding conversation record
    conv_id_str = f"channel_{channel_id}"
    conversation, _ = Conversation.get_or_create(
        conversation_id_str=conv_id_str,
        defaults={'type': 'channel'}
    )

    # Fetch the specific mention records for this user and conversation.
    mentions_to_clear = list(
        Mention.select()
        .join(Message)
        .where((Message.conversation == conversation) & (Mention.user == g.user))
    )

    # Get the IDs of the messages to highlight in the template.
    mention_message_ids = {m.message_id for m in mentions_to_clear}

    # If there are mentions, delete them using their composite primary key.
    if mentions_to_clear:
        # Create a list of individual expressions. Each expression is a complete
        # condition for one row, e.g., (Mention.user == 1) & (Mention.message == 123)
        expressions = [
            (Mention.user == m.user_id) & (Mention.message == m.message_id)
            for m in mentions_to_clear
        ]
        
        # Use reduce() with operator.or_ to chain the expressions together,
        # creating a single WHERE clause like: (expr1) OR (expr2) OR ...
        where_clause = reduce(operator.or_, expressions)
        
        # Execute the single DELETE query.
        Mention.delete().where(where_clause).execute()

    # Mark conversation as read
    status, _ = UserConversationStatus.get_or_create(user=g.user, conversation=conversation)
    last_read_timestamp = status.last_read_timestamp
    status.last_read_timestamp = datetime.datetime.now()
    status.save()

    # Get the messages and count of the members for the template
    messages = (Message.select()
                .where(Message.conversation == conversation)
                .order_by(Message.created_at.asc()))
    members_count = ChannelMember.select().where(ChannelMember.channel == channel).count()

    # We will use a header and a message template to display with HTMX OOB swaps
    header_html = render_template(
        'partials/channel_header.html',
        channel=channel,
        members_count=members_count
    )
    messages_html = render_template(
        'partials/channel_messages.html',
        channel=channel,
        messages=messages,
        last_read_timestamp=last_read_timestamp,
        mention_message_ids=mention_message_ids
    )

    # After marking as read, send back a command to clear the badge
    clear_badge_html = render_template('partials/clear_badge.html',
                                       conv_id_str=conv_id_str,
                                       hx_get_url=url_for('main.get_channel_chat', channel_id=channel.id),
                                       link_text=f"# {channel.name}")

    # Add the HX-Trigger header to fire the custom even on the client (scrolling-chat window)
    response = make_response(header_html + messages_html + clear_badge_html)
    response.headers['HX-Trigger'] = 'load-chat-history'

    return response

@main_bp.route('/chat/channel/<int:channel_id>/members', methods=['GET'])
@login_required
def get_manage_members_view(channel_id):
    """Renders the HTMX partial for the 'manage members' modal."""
    channel = Channel.get_or_none(id=channel_id)
    if not channel:
        return "Channel not found", 404

    # Verify user is a member of the channel they are trying to manage
    if not ChannelMember.get_or_none(user=g.user, channel=channel):
        return "You are not a member of this channel.", 403

    # Find users who are NOT already members of this channel
    subquery = ChannelMember.select(ChannelMember.user_id).where(ChannelMember.channel_id == channel_id)
    users_to_invite = (User.select()
                       .join(WorkspaceMember)
                       .where(User.id.not_in(subquery), WorkspaceMember.workspace == channel.workspace))

    current_members = ChannelMember.select().where(ChannelMember.channel == channel)

    return render_template('partials/manage_members_modal.html', 
                           channel=channel, 
                           users_to_invite=users_to_invite, 
                           current_members=current_members)


@main_bp.route('/chat/channel/<int:channel_id>/members', methods=['POST'])
@login_required
def add_channel_member(channel_id):
    """Processes adding a new member to a channel."""
    user_id_to_add = request.form.get('user_id')
    channel = Channel.get_or_none(id=channel_id)

    if not user_id_to_add or not channel:
        return "Invalid request", 400
    
    # Add the user to the channel
    ChannelMember.get_or_create(user_id=user_id_to_add, channel_id=channel_id)

    # --- Powerful HTMX Response ---
    # We will send back TWO pieces of OOB content:
    # 1. The updated member count for the main chat header.
    # 2. The refreshed content for the modal itself.

    # Re-query the data needed for the modal partial
    subquery = ChannelMember.select(ChannelMember.user_id).where(ChannelMember.channel_id == channel_id)
    users_to_invite = (User.select()
                       .join(WorkspaceMember)
                       .where(User.id.not_in(subquery), WorkspaceMember.workspace == channel.workspace))
    current_members = ChannelMember.select().where(ChannelMember.channel == channel)
    
    # Render the new state of the modal content
    modal_html = render_template('partials/manage_members_modal.html', 
                                 channel=channel, 
                                 users_to_invite=users_to_invite, 
                                 current_members=current_members)
    
    # Render just the new member count for the header
    members_count = current_members.count()
    header_count_html = f'<span id="member-count" hx-swap-oob="innerHTML:#member-count">{members_count} members</span>'

    # Combine them and send the response
    return make_response(modal_html + header_count_html)

'''
@main_bp.route('/chat/channel/<int:channel_id>/invite', methods=['GET'])
@login_required
def get_invite_form(channel_id):
    """Returns the HTMX partial for the user invite form."""
    channel = Channel.get_or_none(id=channel_id)
    if not channel or not channel.is_private:
        return "", 404

    # Find users who are NOT already members of this channel
    subquery = ChannelMember.select(ChannelMember.user_id).where(ChannelMember.channel_id == channel_id)
    users_to_invite = User.select().where(User.id.not_in(subquery))

    return render_template('partials/invite_form.html', channel=channel, users_to_invite=users_to_invite)


@main_bp.route('/chat/channel/<int:channel_id>/invite', methods=['POST'])
@login_required
def invite_user_to_channel(channel_id):
    """Processes the invitation form submission."""
    user_id_to_add = request.form.get('user_id')

    # Simple validation
    if not user_id_to_add:
        return "Please select a user.", 400

    # Create the membership record
    ChannelMember.create(user_id=user_id_to_add, channel_id=channel_id)

    # Return a simple success message that replaces the form
    return f'<div class="text-success my-3">User has been invited!</div>'
'''

# --- Route to get the channel creation form ---
@main_bp.route('/chat/channels/create', methods=['GET'])
@login_required
def get_create_channel_form():
    """Renders the HTMX partial for the channel creation form."""
    return render_template('partials/create_channel_form.html')


# --- Route to handle channel creation ---
@main_bp.route('/chat/channels/create', methods=['POST'])
@login_required
def create_channel():
    """Processes the new channel form submission."""
    channel_name = request.form.get('name', '').strip()
    is_private = request.form.get('is_private') == 'on'

    # --- Validation ---
    # Sanitize name: lowercase, no spaces, limited special chars
    channel_name = re.sub(r'[^a-zA-Z0-9_-]', '', channel_name).lower()

    if not channel_name or len(channel_name) < 3:
        error = "Name must be at least 3 characters long and contain only letters, numbers, underscores, or hyphens."
        return render_template('partials/create_channel_form.html', error=error, name=channel_name, is_private=is_private), 400

    # Assume user belongs to the first workspace they are a member of.
    # In a multi-workspace app, this might come from the URL or session.
    workspace_member = WorkspaceMember.get_or_none(user=g.user)
    if not workspace_member:
        return "You are not a member of any workspace.", 403
    workspace = workspace_member.workspace

    # --- Database Creation ---
    try:
        with db.atomic(): # Use a transaction
            new_channel = Channel.create(
                workspace=workspace,
                name=channel_name,
                is_private=is_private
            )
            # The creator automatically becomes a member
            ChannelMember.create(user=g.user, channel=new_channel)

    except IntegrityError:
        # This happens if the UNIQUE constraint on (workspace, name) fails
        error = f"A channel named '#{channel_name}' already exists."
        return render_template('partials/create_channel_form.html', error=error, name=channel_name, is_private=is_private), 409

    # --- HTMX Success Response ---
    # 1. Render the new channel item to be appended to the list
    new_channel_html = render_template('partials/channel_list_item.html', channel=new_channel)
    # 2. Create a response and add a trigger to close the modal
    response = make_response(new_channel_html)
    response.headers['HX-Trigger'] = 'close-modal'
    return response


@main_bp.route('/chat/dms/start', methods=['GET'])
@login_required
def get_start_dm_form():
    """Gets the list of users a new DM can be started with."""
    # 1. Get the IDs of users the current user ALREADY has a DM with.
    dm_conversations = (Conversation.select()
                        .join(UserConversationStatus)
                        .where((UserConversationStatus.user == g.user) & (Conversation.type == 'dm')))

    existing_partner_ids = {g.user.id} # Always exclude the user themselves
    for conv in dm_conversations:
        user_ids = [int(uid) for uid in conv.conversation_id_str.split('_')[1:]]
        partner_id = next((uid for uid in user_ids if uid != g.user.id), None)
        if partner_id:
            existing_partner_ids.add(partner_id)

    # 2. Select all users whose IDs are NOT in our exclusion list.
    users_to_start_dm = User.select().where(User.id.not_in(list(existing_partner_ids)))

    return render_template('partials/start_dm_modal.html', users_to_start_dm=users_to_start_dm)


# --- MESSAGE EDIT AND DELETE ROUTES ---
@main_bp.route('/chat/message/<int:message_id>', methods=['GET'])
@login_required
def get_message_view(message_id):
    """Returns the standard, read-only view of a single message."""
    message = Message.get_or_none(id=message_id)

    if not message:
        return "", 404

    # This is used by the "Cancel" button on the edit form.
    return render_template('partials/message.html', message=message)


@main_bp.route('/chat/message/<int:message_id>/edit', methods=['GET'])
@login_required
def get_edit_message_form(message_id):
    message = Message.get_or_none(id=message_id)
    if not message or message.user.id != g.user.id:
        return "", 403
    return render_template('partials/edit_message_form.html', message=message)


@main_bp.route('/chat/message/<int:message_id>', methods=['PUT'])
@login_required
def update_message(message_id):
    """
    Handles the submission of an edited message.
    """
    message = Message.get_or_none(id=message_id)
    if not message or message.user.id != g.user.id:
        return "Unauthorized", 403

    new_content = request.form.get('content')
    if new_content:
        # Update the message in the database
        message.content = new_content
        message.is_edited = True
        message.save()

        # Get the conversation ID string for the broadcast
        conv_id_str = message.conversation.conversation_id_str

        # Render the updated message partial
        updated_message_html = render_template('partials/message.html', message=message)

        # Construct the OOB swap HTML for the broadcast. This tells all
        # clients to replace the message's outer HTML with the updated version.
        broadcast_html = f'<div id="message-{message.id}" hx-swap-oob="outerHTML">{updated_message_html}</div>'

        # Broadcast the HTML fragment to all subscribers of the conversation
        chat_manager.broadcast(conv_id_str, broadcast_html)

    # The original hx-put request also needs a response. Return the updated partial.
    return render_template('partials/message.html', message=message)


@main_bp.route('/chat/message/<int:message_id>', methods=['DELETE'])
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


@main_bp.route('/chat/input/default')
@login_required
def get_default_chat_input():
    """Serves the default chat input form."""
    return render_template('partials/chat_input_default.html')


@main_bp.route('/chat/message/<int:message_id>/reply')
@login_required
def get_reply_chat_input(message_id):
    message_to_reply_to = Message.get_or_none(id=message_id)
    if not message_to_reply_to:
        return "Message not found", 404
    return render_template('partials/chat_input_reply.html', message=message_to_reply_to)


# --- PROFILE EDITING ROUTES ---
@main_bp.route('/profile/address/view', methods=['GET'])
@login_required
def get_address_display():
    """Returns the read-only address display partial."""
    return render_template('partials/address_display.html', user=g.user)

@main_bp.route('/profile/address/edit', methods=['GET'])
@login_required
def get_address_form():
    """Returns the address editing form partial."""
    return render_template('partials/address_form.html', user=g.user)

@main_bp.route('/profile/address', methods=['PUT'])
@login_required
def update_address():
    """Processes the address form submission."""
    user = g.user
    user.country = request.form.get('country')
    user.city = request.form.get('city')
    user.timezone = request.form.get('timezone')
    user.save()

    # IMPORTANT: Update the header and then return the display partial
    header_html = render_template('partials/profile_header_oob.html', user=user)
    display_html = render_template('partials/address_display.html', user=user)

    return make_response(header_html + display_html)


@main_bp.route('/test-chat')
@login_required
def test_chat():
    return render_template('websocket_test.html')

# --- Admin Routes ---

@admin_bp.route('/users')
def list_users():
    users = User.select()
    return render_template('admin/user_list.html', users=users)

@admin_bp.route('/users/create', methods=['GET'])
def create_user_form():
    return render_template('admin/create_user.html')

@admin_bp.route('/users/create', methods=['POST'])
def create_user():
    username = request.form.get('username')
    email = request.form.get('email')
    if username and email:
        User.create(username=username, email=email)
        return redirect(url_for('admin.list_users'))
    return redirect(url_for('admin.create_user_form'))

# --- WebSocket Route ---
@main_bp.route('/chat/dm/<int:other_user_id>')
@login_required
def get_dm_chat(other_user_id):
    other_user = User.get_or_none(id=other_user_id)
    if not other_user: return "User not found", 404

    # Create the canonical conversation ID string by sorting user IDs
    user_ids = sorted([g.user.id, other_user.id])
    conv_id_str = f"dm_{user_ids[0]}_{user_ids[1]}"

    conversation, _ = Conversation.get_or_create(
        conversation_id_str=conv_id_str,
        defaults={'type': 'dm'}
    )

    # Ensure a status record exists for the current user
    status, created = UserConversationStatus.get_or_create(user=g.user, conversation=conversation)
    last_read_timestamp = status.last_read_timestamp
    status.last_read_timestamp = datetime.datetime.now()
    status.save()

    # Also ensure a status record exists for the other user
    UserConversationStatus.get_or_create(user=other_user, conversation=conversation)

    messages = (Message.select()
                .where(Message.conversation == conversation)
                .order_by(Message.created_at.asc()))

    # Header and message templates for a HTMX OOB Swap
    header_html = render_template('partials/dm_header.html', other_user=other_user)
    messages_html = render_template('partials/dm_messages.html', messages=messages, other_user=other_user, last_read_timestamp=last_read_timestamp)

    # Clear the new messages badge
    clear_badge_html = ""
    if other_user.id != g.user.id:
        clear_badge_html = render_template('partials/clear_badge.html',
                                       conv_id_str=conv_id_str,
                                       hx_get_url=url_for('main.get_dm_chat', other_user_id=other_user.id),
                                       link_text=other_user.display_name or other_user.username)

    # If a status was just created, it means this user wasn't in the DM list.
    # So, we send an OOB swap to add them.
    add_user_to_sidebar_html = ""
    if created and other_user.id != g.user.id:
        add_user_to_sidebar_html = render_template('partials/dm_list_item_oob.html',
                                                   user=other_user,
                                                   conv_id_str=conv_id_str,
                                                   is_online=other_user.id in chat_manager.online_users)

    # HX-Trigger the chat window to load (allows scrolling to new messages).
    response = make_response(header_html + messages_html + clear_badge_html + add_user_to_sidebar_html)
    response.headers['HX-Trigger'] = 'load-chat-history'

    return response


@main_bp.route('/profile/status', methods=['PUT'])
@login_required
def update_presence_status():
    """Updates the user's presence status and broadcasts the change."""
    new_status = request.form.get('status')
    if new_status and new_status in STATUS_CLASS_MAP:
        user = g.user
        user.presence_status = new_status
        user.save()

        # --- Broadcast the changes to all connected clients ---

        # 1. Broadcast the update for the DM list dots (uses bg-* classes)
        status_class = STATUS_CLASS_MAP.get(new_status, 'bg-secondary')
        dm_list_presence_html = f'<span id="status-dot-{user.id}" class="me-2 rounded-circle {status_class}" style="width: 10px; height: 10px;" hx-swap-oob="true"></span>'
        chat_manager.broadcast_to_all(dm_list_presence_html)

        # 2. [THE FIX] Broadcast a SECOND, separate update for the sidebar profile button
        #    This uses the custom presence-* classes.
        profile_status_map = {'online': 'presence-online', 'away': 'presence-away', 'busy': 'presence-busy'}
        profile_status_class = profile_status_map.get(new_status, 'presence-away')
        sidebar_presence_html = f'<span id="sidebar-presence-indicator-{user.id}" class="presence-indicator {profile_status_class}" hx-swap-oob="true"></span>'
        chat_manager.broadcast_to_all(sidebar_presence_html)

        # 3. Also update the indicator on the profile page itself (if other tabs are open)
        profile_page_presence_html = f'<span id="profile-presence-indicator-{user.id}" class="presence-indicator {profile_status_class}" hx-swap-oob="true"></span>'
        chat_manager.broadcast_to_all(profile_page_presence_html)

        # Return the updated profile header to the user who made the change
        return render_template('partials/profile_header.html', user=user)

    return "Invalid status", 400


@main_bp.route('/profile/theme', methods=['PUT'])
@login_required
def update_theme():
    """Updates the user's theme preference."""
    new_theme = request.form.get('theme')
    if new_theme in ['light', 'dark', 'system']:
        user = g.user
        user.theme = new_theme
        user.save()
        # Instruct the browser to do a full reload to apply the new theme
        response = make_response("")
        response.headers['HX-Refresh'] = 'true'
        return response
    return "Invalid theme", 400


@main_bp.route('/chat/user/preference/wysiwyg', methods=['PUT'])
@login_required
def set_wysiwyg_preference():
    """Updates the user's preference for the WYSIWYG editor."""
    # The value comes from our JS, default to 'false' if not provided
    enabled_str = request.form.get('wysiwyg_enabled', 'false')
    enabled = enabled_str.lower() == 'true'

    # Update the user record only if the value has changed
    if g.user.wysiwyg_enabled != enabled:
        user = User.get_by_id(g.user.id)
        user.wysiwyg_enabled = enabled
        user.save()
        # g.user is a snapshot from the start of the request,
        # so we update it too for the current request context.
        g.user.wysiwyg_enabled = enabled

    # Return a 204 No Content response, as HTMX doesn't need to swap anything
    return '', 204


@main_bp.route('/chat/message/<int:message_id>/load_for_edit')
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
            'partials/chat_input_edit.html',
            message=message,
            message_content_html=message_content_html
        )
    except Message.DoesNotExist:
        return "Message not found", 404
    return render_template('partials/chat_input_edit.html', message=message)


# --- WebSocket Handler ---
@sock.route('/ws/chat')
def chat(ws):
    print("INFO: WebSocket client connected.")
    user = session.get('user_id') and User.get_or_none(id=session.get('user_id'))
    if not user:
        print("ERROR: Unauthenticated user tried to connect. Closing.")
        ws.close(reason=1008, message="Not authenticated")
        return
    ws.user = user

    chat_manager.set_online(user.id, ws)

    # When a user connects, broadcast their ACTUAL saved status, not just "online".
    status_class = STATUS_CLASS_MAP.get(user.presence_status, 'bg-secondary')
    presence_html = f'<span id="status-dot-{user.id}" class="me-2 rounded-circle {status_class}" style="width: 10px; height: 10px;" hx-swap-oob="true"></span>'
    chat_manager.broadcast_to_all(presence_html)

    try:
        while True:
            data = json.loads(ws.receive())
            event_type = data.get("type")

            if event_type in ['typing_start', 'typing_stop']:
                is_typing = event_type == 'typing_start'
                indicator_html = f'<div id="typing-indicator" hx-swap-oob="true">{f"<p>{ws.user.username} is typing...</p>" if is_typing else ""}</div>'
                chat_manager.broadcast(data.get('conversation_id'), indicator_html, sender_ws=ws)
                continue

            if event_type == 'subscribe':
                conv_id_str = data.get('conversation_id')
                if conv_id_str: chat_manager.subscribe(conv_id_str, ws)
                continue

            chat_text = data.get('chat_message')
            parent_id = data.get('parent_message_id')
            conv_id_str = getattr(ws, 'channel_id', None)

            if not (chat_text and conv_id_str): continue

            conversation = Conversation.get_or_none(conversation_id_str=conv_id_str)
            if not conversation: continue

            with db.atomic():
                new_message = Message.create(
                    user=ws.user,
                    conversation=conversation,
                    content=chat_text,
                    parent_message=parent_id if parent_id else None
                )
                mentioned_usernames = set(re.findall(r'@(\w+)', chat_text))
                if mentioned_usernames:
                    mentioned_users = User.select().where(User.username.in_(list(mentioned_usernames)))
                    for mentioned_user in mentioned_users:
                        Mention.get_or_create(user=mentioned_user, message=new_message)

            if conversation.type == 'dm' and Message.select().where(Message.conversation == conversation).count() == 1:
                user_ids = [int(uid) for uid in conv_id_str.split('_')[1:]]
                recipient_id = next((uid for uid in user_ids if uid != ws.user.id), None)
                if recipient_id and recipient_id in chat_manager.all_clients:
                    add_sender_html = render_template('partials/dm_list_item_oob.html', user=ws.user, conv_id_str=conv_id_str, is_online=True)
                    chat_manager.all_clients[recipient_id].send(add_sender_html)

            current_time = datetime.datetime.now()
            if conv_id_str in chat_manager.active_connections:
                with db.atomic():
                    for viewer_ws in chat_manager.active_connections[conv_id_str]:
                        (UserConversationStatus
                         .update(last_read_timestamp=current_time)
                         .where((UserConversationStatus.user == viewer_ws.user) & (UserConversationStatus.conversation == conversation))
                         .execute())

            # 1. Render the new message partial once.
            new_message_html = render_template('partials/message.html', message=new_message)

            # 2. This is the OOB fragment to append the message to the viewer's list.
            message_to_broadcast = f'<div hx-swap-oob="beforeend:#message-list">{new_message_html}</div>'

            # 3. Broadcast the message ONLY to other clients viewing the channel.
            #    This prevents the oobErrorNoTarget by not sending to users who don't have #message-list rendered.
            chat_manager.broadcast(conv_id_str, message_to_broadcast, sender_ws=ws)

            # 4. The original sender also needs to receive the message.
            #    We also check if they were replying, and if so, tack on the command to reset their input field.
            message_for_sender = message_to_broadcast
            if parent_id:
                input_html = render_template('partials/chat_input_default.html')
                message_for_sender += f'<div id="chat-input-container" hx-swap-oob="outerHTML">{input_html}</div>'
            ws.send(message_for_sender)

            if conversation.type == 'channel':
                channel_id = conversation.conversation_id_str.split('_')[1]
                channel = Channel.get_by_id(channel_id)
                members = User.select().join(ChannelMember).where(ChannelMember.channel == channel)
            else:
                user_ids = [int(uid) for uid in conv_id_str.split('_')[1:]]
                members = User.select().where(User.id.in_(user_ids))

            for member in members:
                # Don't notify the person who sent the message or users who aren't connected
                if member.id == ws.user.id or member.id not in chat_manager.all_clients:
                    continue

                member_ws = chat_manager.all_clients[member.id]

                # If the user is actively viewing this conversation, do nothing.
                if getattr(member_ws, 'channel_id', None) == conv_id_str:
                    continue

                # This user is eligible for notifications. Get their status record.
                status, _ = UserConversationStatus.get_or_create(user=member, conversation=conversation)

                # 1. Handle UI badge/bolding updates (this part is mostly the same)
                notification_html = None
                if conversation.type == 'channel':
                    channel_model = Channel.get_by_id(conversation.conversation_id_str.split('_')[1])
                    link_text = f"# {channel_model.name}"
                    hx_get_url = url_for('main.get_channel_chat', channel_id=channel_model.id)
                    new_mention_count = Mention.select().join(Message).where((Message.created_at > status.last_read_timestamp) & (Mention.user == member) & (Message.conversation == conversation)).count()
                    if new_mention_count > 0:
                        total_mentions = Mention.select().join(Message).where((Mention.user == member) & (Message.conversation == conversation)).count()
                        notification_html = render_template('partials/unread_badge.html', conv_id_str=conv_id_str, count=total_mentions, link_text=link_text, hx_get_url=hx_get_url)
                    elif Message.select().where((Message.conversation == conversation) & (Message.created_at > status.last_read_timestamp)).exists():
                        notification_html = render_template('partials/bold_link.html', conv_id_str=conv_id_str, link_text=link_text, hx_get_url=hx_get_url)
                else: # DM
                    link_text = ws.user.display_name or ws.user.username
                    hx_get_url = url_for('main.get_dm_chat', other_user_id=ws.user.id)
                    new_count = Message.select().where((Message.conversation == conversation) & (Message.created_at > status.last_read_timestamp) & (Message.user != member)).count()
                    if new_count > 0:
                        notification_html = render_template('partials/unread_badge.html', conv_id_str=conv_id_str, count=new_count, link_text=link_text, hx_get_url=hx_get_url)

                if notification_html:
                    member_ws.send(notification_html)

                # 2. Handle Desktop Notification with Cooldown
                now = datetime.datetime.now()
                send_notification = False
                if status.last_notified_timestamp is None or (now - status.last_notified_timestamp) > datetime.timedelta(seconds=60):
                    send_notification = True

                if send_notification:
                    notification_payload = {
                        "type": "notification",
                        "title": f"New message from {new_message.user.display_name or new_message.user.username}",
                        "body": new_message.content,
                        "icon": url_for('static', filename='favicon.ico', _external=True),
                        "tag": conv_id_str
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
        print(f"ERROR: An exception occurred for user '{getattr(ws, 'user', 'unknown')}': {e}")
    finally:
        if hasattr(ws, 'user') and ws.user:
            chat_manager.set_offline(ws.user.id)
            presence_html = f'<span id="status-dot-{ws.user.id}" class="me-2 rounded-circle bg-secondary" style="width: 10px; height: 10px;" hx-swap-oob="true"></span>'
            chat_manager.broadcast_to_all(presence_html)
            chat_manager.unsubscribe(ws)
            print(f"INFO: Client connection closed for '{ws.user.username}'.")
