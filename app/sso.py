import datetime

from authlib.integrations.flask_client import OAuth
from flask import url_for, session, redirect, render_template, current_app
from .models import (
    User,
    db,
    Workspace,
    Channel,
    ChannelMember,
    WorkspaceMember,
    Conversation,
    UserConversationStatus,
    Mention,
)
from .chat_manager import chat_manager

oauth = OAuth()


def init_sso(app):
    """
    Initializes the OAuth client for SSO.
    """
    oauth.init_app(app)

    # The 'name' must match the provider you register below
    oauth.register(
        name="authentik",  # You can name this whatever you like
        client_id=app.config.get("OIDC_CLIENT_ID"),
        client_secret=app.config.get("OIDC_CLIENT_SECRET"),
        server_metadata_url=f"{app.config.get('OIDC_ISSUER_URL')}.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


def handle_auth_callback():
    """
    Handles the authentication callback from the SSO provider.
    Creates or updates a user and logs them in.
    """

    current_app.logger.info(f"Session contents on callback: {dict(session)}")

    # Fetch the token from the provider
    token = oauth.authentik.authorize_access_token()

    # Retrieve the nonce from the session. .pop() removes it so it can't be used again.
    nonce = session.pop("nonce", None)

    # Get user info from the 'userinfo' endpoint or from the 'id_token'
    user_info = oauth.authentik.parse_id_token(token, nonce=nonce)

    sso_id = user_info.get("sub")  # 'sub' is the standard OIDC subject identifier
    email = user_info.get("email")
    username = email.split("@")[0].lower().replace(".", "_")
    display_name = user_info.get("given_name")

    if not sso_id or not email:
        return redirect(
            url_for(
                "main.login_page",
                error="SSO provider did not return required information.",
            )
        )

    # --- Let's setup or create this user ---
    # Try to find the user by their unique SSO ID first. This is the most reliable.
    user = User.get_or_none(User.sso_id == sso_id)

    with db.atomic():
        if user is None:
            # No user found by SSO ID. Check if one exists with this email but no SSO ID.
            user = User.get_or_none((User.email == email) & (User.sso_id.is_null()))

            if user:  # User exists, link the account
                print(f"Linking existing user '{user.username}' via SSO.")
                user.sso_id = sso_id
                user.sso_provider = "authentik"
                # Optionally update their name from SSO
                user.display_name = display_name
                user.save()

            else:  # User does not exist, create a new one
                print(f"Creating a new user '{username}' from SSO login.")
                user = User.create(
                    sso_id=sso_id,
                    email=email,
                    username=username,
                    display_name=display_name,
                    sso_provider="authentik",
                    is_active=True,
                    last_threads_view_at=datetime.datetime.now(),
                )

            # 1. Add to Workspace and Broadcast
            default_workspace = Workspace.get_or_none(Workspace.name == "DevOcho")
            if default_workspace:
                WorkspaceMember.create(
                    user=user, workspace=default_workspace, role="member"
                )
                print(
                    f"-> Added '{user.username}' to workspace '{default_workspace.name}'."
                )

                # 2. Add to Default Channels
                print("-> Searching for default channels...")
                default_channels = Channel.select().where(
                    (Channel.name.in_(["general", "announcements"]))
                    & (Channel.workspace == default_workspace)
                )

                if not default_channels.exists():
                    print(
                        "-> WARNING: Default channels 'general' or 'announcements' not found!"
                    )
                else:
                    for channel in default_channels:
                        ChannelMember.create(user=user, channel=channel)
                        print(
                            f"-> Added '{user.username}' to default channel '#{channel.name}'."
                        )
            else:
                print(
                    f"-> WARNING: Default workspace 'DevOcho' not found. Could not process new user '{user.username}'."
                )

        else:
            # User was found by sso_id, update their details just in case they changed.
            user.email = email
            user.username = username
            user.display_name = display_name
            user.save()

    # Store user ID in the session to log them in
    session["user_id"] = user.id

    return redirect(url_for("main.chat_interface"))
