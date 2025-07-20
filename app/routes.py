from flask import Blueprint, render_template, request, redirect, url_for, session, g, make_response
from .models import User, Channel, ChannelMember, Message, Conversation
from .sso import oauth # Import the oauth object
import functools
import secrets
from . import sock
from .chat_manager import chat_manager
import json

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
    # Fetch channels the current user is a member of
    user_channels = (Channel.select()
                     .join(ChannelMember)
                     .where(ChannelMember.user == g.user))

    # Fetch all users for the Direct Messages list (excluding the current user)
    other_users = User.select().where(User.id != g.user.id)

    return render_template('chat.html',
                           channels=user_channels,
                           direct_message_users=other_users,
                           online_users=chat_manager.online_users)

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

    messages = (Message.select()
                .where(Message.conversation == conversation)
                .order_by(Message.created_at.asc()))

    # Add the HX-Trigger header to fire the custom even on the client (scrolling-chat window)
    html = render_template('partials/channel_chat.html', channel=channel, messages=messages)
    response = make_response(html)
    response.headers['HX-Trigger'] = 'load-chat-history'

    return response

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

    messages = (Message.select()
                .where(Message.conversation == conversation)
                .order_by(Message.created_at.asc()))

    # HX-Trigger the chat window to load.
    html = render_template('partials/dm_chat.html', messages=messages, other_user=other_user)
    response = make_response(html)
    response.headers['HX-Trigger'] = 'load-chat-history'

    return response


# --- MODIFIED: WebSocket Handler ---
@sock.route('/ws/chat')
def chat(ws):
    print("INFO: WebSocket client connected.")
    user = session.get('user_id') and User.get_or_none(id=session.get('user_id'))
    if not user:
        print("ERROR: Unauthenticated user tried to connect. Closing.")
        ws.close(reason=1008, message="Not authenticated")
        return
    ws.user = user

    # Mark user online and broadcast
    chat_manager.set_online(user.id, ws)
    presence_html = f'<span id="status-dot-{user.id}" class="me-2 rounded-circle bg-success" style="width: 10px; height: 10px;" hx-swap-oob="true"></span>'
    chat_manager.broadcast_to_all(presence_html)

    try:
        while True:
            data = json.loads(ws.receive())

            # --- HANDLE TYPING INDICATORS ---
            if data.get('type') == 'typing_start':
                indicator_html = f'<div id="typing-indicator" hx-swap-oob="true"><p>{ws.user.username} is typing...</p></div>'
                chat_manager.broadcast(data.get('conversation_id'), indicator_html, sender_ws=ws)
                continue

            if data.get('type') == 'typing_stop':
                indicator_html = '<div id="typing-indicator" hx-swap-oob="true"></div>'
                chat_manager.broadcast(data.get('conversation_id'), indicator_html, sender_ws=ws)
                continue

            # --- HANDLE SUBSCRIPTION ---
            if data.get('type') == 'subscribe':
                conv_id_str = data.get('conversation_id')
                if conv_id_str:
                    chat_manager.subscribe(conv_id_str, ws)
                continue

            # --- HANDLE CHAT MESSAGES ---
            chat_text = data.get('chat_message')
            current_conversation_id_str = getattr(ws, 'channel_id', None)
            if not (chat_text and current_conversation_id_str): continue

            conversation = Conversation.get_or_none(conversation_id_str=current_conversation_id_str)
            if not conversation: continue

            new_message = Message.create(user=ws.user, conversation=conversation, content=chat_text)
            message_html = f"""<div id="messages-container" hx-swap-oob="beforeend">{render_template('partials/message.html', message=new_message)}</div>"""
            chat_manager.broadcast(current_conversation_id_str, message_html)

    except Exception as e:
        print(f"ERROR: An exception occurred for user '{ws.user.username}': {e}")
    finally:
        # --- PRESENCE: Mark user offline and broadcast ---
        chat_manager.set_offline(ws.user.id)
        presence_html = f'<span id="status-dot-{ws.user.id}" class="me-2 rounded-circle bg-secondary" style="width: 10px; height: 10px;" hx-swap-oob="true"></span>'
        chat_manager.broadcast_to_all(presence_html)

        chat_manager.unsubscribe(ws)
        print(f"INFO: Client connection closed for '{ws.user.username}'.")
