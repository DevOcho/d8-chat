from flask import Blueprint, render_template, request, redirect, url_for, session, g, make_response
from .models import User, Channel, ChannelMember, Message, Conversation, Workspace, WorkspaceMember, db, UserConversationStatus
from .sso import oauth # Import the oauth object
import functools
import secrets
from . import sock
from .chat_manager import chat_manager
import json
from peewee import IntegrityError, fn
import re
import datetime

# Main blueprint for general app routes
main_bp = Blueprint('main', __name__)

# Admin blueprint for admin-specific routes
admin_bp = Blueprint('admin', __name__)

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
    return render_template('profile.html', user=g.user)


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
                       unread_counts=unread_counts)


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

    # --- Mark conversation as read ---
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
        last_read_timestamp=last_read_timestamp
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
    response.headers['HX-Trigger'] = 'close-create-channel-modal'
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

    '''
    # --- Mark conversation as read ---
    status, _ = UserConversationStatus.get_or_create(user=g.user, conversation=conversation)
    last_read_timestamp = status.last_read_timestamp
    status.last_read_timestamp = datetime.datetime.now()
    status.save()
    '''

    messages = (Message.select()
                .where(Message.conversation == conversation)
                .order_by(Message.created_at.asc()))

    # Header and message templates for a HTMX OOB Swap
    header_html = render_template('partials/dm_header.html', other_user=other_user)
    messages_html = render_template('partials/dm_messages.html', messages=messages, other_user=other_user, last_read_timestamp=last_read_timestamp)

    # Clear the new messages badge
    clear_badge_html = render_template('partials/clear_badge.html',
                                       conv_id_str=conv_id_str,
                                       hx_get_url=url_for('main.get_dm_chat', other_user_id=other_user.id),
                                       link_text=other_user.username)

    # If a status was just created, it means this user wasn't in the DM list.
    # So, we send an OOB swap to add them.
    add_user_to_sidebar_html = ""
    if created:
        add_user_to_sidebar_html = render_template('partials/dm_list_item_oob.html',
                                                   user=other_user,
                                                   conv_id_str=conv_id_str,
                                                   is_online=other_user.id in chat_manager.online_users)

    # HX-Trigger the chat window to load (allows scrolling to new messages).
    response = make_response(header_html + messages_html + clear_badge_html)
    response.headers['HX-Trigger'] = 'load-chat-history'

    return response


# --- FULL AND CORRECTED WebSocket Handler ---
@sock.route('/ws/chat')
def chat(ws):
    print("INFO: WebSocket client connected.")
    # Authenticate the user based on the session
    user = session.get('user_id') and User.get_or_none(id=session.get('user_id'))
    if not user:
        print("ERROR: Unauthenticated user tried to connect. Closing.")
        ws.close(reason=1008, message="Not authenticated")
        return
    ws.user = user

    # Mark user as online and broadcast their presence to everyone
    chat_manager.set_online(user.id, ws)
    presence_html = f'<span id="status-dot-{user.id}" class="me-2 rounded-circle bg-success" style="width: 10px; height: 10px;" hx-swap-oob="true"></span>'
    chat_manager.broadcast_to_all(presence_html)

    try:
        # Main loop to listen for messages from this specific client
        while True:
            data = json.loads(ws.receive())
            event_type = data.get("type")

            # --- HANDLE TYPING INDICATORS ---
            if event_type == 'typing_start':
                indicator_html = f'<div id="typing-indicator" hx-swap-oob="true"><p>{ws.user.username} is typing...</p></div>'
                chat_manager.broadcast(data.get('conversation_id'), indicator_html, sender_ws=ws)
                continue

            if event_type == 'typing_stop':
                indicator_html = '<div id="typing-indicator" hx-swap-oob="true"></div>'
                chat_manager.broadcast(data.get('conversation_id'), indicator_html, sender_ws=ws)
                continue

            # --- HANDLE CHANNEL SUBSCRIPTION ---
            if event_type == 'subscribe':
                conv_id_str = data.get('conversation_id')
                if conv_id_str:
                    chat_manager.subscribe(conv_id_str, ws)
                continue

            # --- HANDLE NEW CHAT MESSAGES ---
            chat_text = data.get('chat_message')
            parent_id = data.get('parent_message_id')
            conv_id_str = getattr(ws, 'channel_id', None)

            if not (chat_text and conv_id_str):
                continue

            conversation = Conversation.get_or_none(conversation_id_str=conv_id_str)
            if not conversation:
                continue

            # Create the new message in the database
            new_message = Message.create(
                user=ws.user,
                conversation=conversation,
                content=chat_text,
                parent_message=parent_id if parent_id else None
            )

            # Check if this is the first message in a DM conversation
            if conversation.type == 'dm':
                message_count = Message.select().where(Message.conversation == conversation).count()
                if message_count == 1:
                    # This is the first message. We need to update the recipient's sidebar.
                    user_ids = [int(uid) for uid in conversation.conversation_id_str.split('_')[1:]]
                    recipient_id = next((uid for uid in user_ids if uid != ws.user.id), None)

                    if recipient_id and recipient_id in chat_manager.all_clients:
                        # Render the sidebar item from the recipient's perspective
                        # The 'user' is the person who sent the message (ws.user)
                        add_sender_html = render_template('partials/dm_list_item_oob.html',
                                                          user=ws.user,
                                                          conv_id_str=conv_id_str,
                                                          is_online=ws.user.id in chat_manager.online_users)
                        # Send this command only to the recipient
                        chat_manager.all_clients[recipient_id].send(add_sender_html)

            # Immediately update the read timestamp for everyone currently viewing this conversation.
            # This prevents unread counts from incrementing for users who are actively watching.
            current_time = datetime.datetime.now()
            if conv_id_str in chat_manager.active_connections:
                # Use a transaction for efficiency if many users are in the channel
                with db.atomic():
                    for viewer_ws in chat_manager.active_connections[conv_id_str]:
                        status, _ = UserConversationStatus.get_or_create(
                            user=viewer_ws.user,
                            conversation=conversation)
                        status.last_read_timestamp = current_time
                        status.save()

            # 1. Broadcast the new message HTML to everyone in the conversation
            message_html = f"""<div id="message-list" hx-swap-oob="beforeend">{render_template('partials/message.html', message=new_message)}</div>"""
            chat_manager.broadcast(conv_id_str, message_html)

            # 2. If it was a reply, reset the sender's input form
            if parent_id:
                input_html = render_template('partials/chat_input_default.html')
                reset_input_command = f"""<div id="chat-input-container" hx-swap-oob="outerHTML">{input_html}</div>"""
                ws.send(reset_input_command)

            # 3. Broadcast real-time unread notifications to other users
            # Find all members of this conversation
            if conversation.type == 'channel':
                channel_id = conversation.conversation_id_str.split('_')[1]
                channel = Channel.get_by_id(channel_id)
                members = User.select().join(ChannelMember).where(ChannelMember.channel == channel)
                #link_text = f"# {channel.name}"
                #hx_get_url = url_for('main.get_channel_chat', channel_id=channel.id)
            else: # It's a DM
                user_ids = [int(uid) for uid in conversation.conversation_id_str.split('_')[1:]]
                members = User.select().where(User.id.in_(user_ids))
                #other_user_id = next(uid for uid in user_ids if uid != ws.user.id)
                #other_user = User.get_by_id(other_user_id)
                #link_text = other_user.username
                #hx_get_url = url_for('main.get_dm_chat', other_user_id=other_user_id)

            # Loop through members to notify them
            for member in members:
                if member.id == ws.user.id:
                    continue  # Don't notify the sender

                # Check if the user is online but NOT viewing this conversation
                is_viewing_channel = member.id in chat_manager.all_clients and getattr(chat_manager.all_clients[member.id], 'channel_id', None) == conv_id_str

                if not is_viewing_channel:
                    # Determine link text and URL based on conversation type
                    if conversation.type == 'channel':
                        channel = Channel.get_by_id(conversation.conversation_id_str.split('_')[1])
                        link_text = f"# {channel.name}"
                        hx_get_url = url_for('main.get_channel_chat', channel_id=channel.id)
                    else: # It's a DM
                        # The link for the recipient should always point to the SENDER.
                        link_text = ws.user.username
                        hx_get_url = url_for('main.get_dm_chat', other_user_id=ws.user.id)

                    status, _ = UserConversationStatus.get_or_create(user=member, conversation=conversation)
                    new_count = Message.select().where(
                        (Message.conversation == conversation) &
                        (Message.created_at > status.last_read_timestamp) &
                        (Message.user != member)
                    ).count()

                    if new_count > 0 and member.id in chat_manager.all_clients:
                        badge_html = render_template('partials/unread_badge.html',
                                                     conv_id_str=conv_id_str,
                                                     count=new_count,
                                                     link_text=link_text,
                                                     hx_get_url=hx_get_url)
                        chat_manager.all_clients[member.id].send(badge_html)

    except Exception as e:
        print(f"ERROR: An exception occurred for user '{getattr(ws, 'user', 'unknown')}': {e}")
    finally:
        # This block runs when the client disconnects or an error occurs
        if ws.user:
            # Mark user as offline and broadcast their presence
            chat_manager.set_offline(ws.user.id)
            presence_html = f'<span id="status-dot-{ws.user.id}" class="me-2 rounded-circle bg-secondary" style="width: 10px; height: 10px;" hx-swap-oob="true"></span>'
            chat_manager.broadcast_to_all(presence_html)
            # Clean up their subscription
            chat_manager.unsubscribe(ws)
            print(f"INFO: Client connection closed for '{ws.user.username}'.")
