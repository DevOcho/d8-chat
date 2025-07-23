from authlib.integrations.flask_client import OAuth
from flask import url_for, session, redirect
from .models import User, db, Workspace, WorkspaceMember

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
    # 1. Try to find the user by their unique SSO ID first. This is the most reliable.
    user = User.get_or_none(User.sso_id == sso_id)

    with db.atomic():
        if user is None:
            # 2. If not found, try to link to an existing user by email.
            # This handles pre-seeded users logging in for the first time.
            user = User.get_or_none((User.email == email) & (User.sso_id.is_null()))

            if user:
                # User found by email, link their account by setting the sso_id
                print(f"Linking SSO ID to existing user '{user.username}' (found by email).")
                user.sso_id = sso_id
                user.sso_provider = 'authentik'
                user.username = username # Update their name from SSO
                user.save()
            else:
                # 3. If still not found, this is a genuinely new user. Create them.
                print(f"Creating a new user '{username}' from SSO login.")
                user = User.create(
                    sso_id=sso_id,
                    email=email,
                    username=username,
                    sso_provider='authentik',
                    is_active=True
                )

                # --- AUTOMATICALLY ADD TO WORKSPACE ---
                # Find the default workspace.
                default_workspace = Workspace.get_or_none(Workspace.name == 'DevOcho')
                if default_workspace:
                    # Add the new user to the workspace as a member.
                    WorkspaceMember.create(
                        user=user,
                        workspace=default_workspace,
                        role='member' # Assign a default role
                    )
                    print(f"Automatically added new user '{user.username}' to workspace '{default_workspace.name}'.")
                else:
                    # This is an important log for debugging if the seed script wasn't run.
                    print(f"WARNING: Default workspace 'DevOcho' not found. Could not add new user '{user.username}'.")

        else:
            # User was found by sso_id, update their details just in case they changed.
            user.email = email
            user.username = username
            user.save()

    # Store user ID in the session to log them in
    session['user_id'] = user.id

    return redirect(url_for('main.profile'))
