from authlib.integrations.flask_client import OAuth
from flask import url_for, session, redirect, render_template
from .models import User, db, Workspace, Channel, ChannelMember, WorkspaceMember, Conversation, UserConversationStatus
from .chat_manager import chat_manager

oauth = OAuth()

def init_sso(app):
    """
    Initializes the OAuth client for SSO.
    """
    oauth.init_app(app)

    # The 'name' must match the provider you register below
    oauth.register(
        name='authentik', # You can name this whatever you like
        client_id=app.config.get('OIDC_CLIENT_ID'),
        client_secret=app.config.get('OIDC_CLIENT_SECRET'),
        server_metadata_url=f"{app.config.get('OIDC_ISSUER_URL')}.well-known/openid-configuration",
        client_kwargs={'scope': 'openid email profile'}
    )

def handle_auth_callback():
    """
    Handles the authentication callback from the SSO provider.
    Creates or updates a user and logs them in.
    """
    # Fetch the token from the provider
    token = oauth.authentik.authorize_access_token()

    # Retrieve the nonce from the session. .pop() removes it so it can't be used again.
    nonce = session.pop('nonce', None)

    # Get user info from the 'userinfo' endpoint or from the 'id_token'
    user_info = oauth.authentik.parse_id_token(token, nonce=nonce)

    sso_id = user_info.get('sub') # 'sub' is the standard OIDC subject identifier
    email = user_info.get('email')
    username = user_info.get('name', email) # Use name, fall back to email

    if not sso_id or not email:
        return redirect(url_for('main.login_page', error="SSO provider did not return required information."))

   # --- Let's setup or create this user ---
    # Try to find the user by their unique SSO ID first. This is the most reliable.
    user = User.get_or_none(User.sso_id == sso_id)

    with db.atomic():
        if user is None:
            # If not found, let's create the user in the system and setup them up.
            user = User.get_or_none((User.email == email) & (User.sso_id.is_null()))
            print(f"Creating a new user '{username}' from SSO login.")
            user = User.create(
                sso_id=sso_id,
                email=email,
                username=username,
                sso_provider='authentik',
                is_active=True
            )

            # 1. Add to Workspace and Broadcast
            default_workspace = Workspace.get_or_none(Workspace.name == 'DevOcho')
            if default_workspace:
                WorkspaceMember.create(user=user, workspace=default_workspace, role='member')
                print(f"-> Added '{user.username}' to workspace '{default_workspace.name}'.")
                new_user_html = render_template('partials/dm_list_item.html', user=user)
                chat_manager.broadcast_to_all(new_user_html)

                # 2. Add to Default Channels
                print("-> Searching for default channels...")
                default_channels = Channel.select().where(
                    (Channel.name.in_(['general', 'announcements'])) &
                    (Channel.workspace == default_workspace)
                )

                if not default_channels.exists():
                    print("-> WARNING: Default channels 'general' or 'announcements' not found!")
                else:
                    for channel in default_channels:
                        ChannelMember.create(user=user, channel=channel)
                        print(f"-> Added '{user.username}' to default channel '#{channel.name}'.")
            else:
                print(f"-> WARNING: Default workspace 'DevOcho' not found. Could not process new user '{user.username}'.")

        else:
            # User was found by sso_id, update their details just in case they changed.
            user.email = email
            user.username = username
            user.save()

    # Store user ID in the session to log them in
    session['user_id'] = user.id

    return redirect(url_for('main.profile'))
