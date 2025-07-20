from authlib.integrations.flask_client import OAuth
from flask import url_for, session, redirect
from .models import User, db

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
        # Handle error: essential info not provided
        return redirect(url_for('main.login_page', error="SSO provider did not return required information."))

    # Find or create the user in the database
    user = User.get_or_none(User.sso_id == sso_id)

    with db.atomic():
        if user is None:
            # User does not exist, create a new one
            user = User.create(
                sso_id=sso_id,
                email=email,
                username=username,
                sso_provider='authentik',
                is_active=True
            )
        else:
            # User exists, update their details if necessary
            user.email = email
            user.username = username
            user.save()

    # Store user ID in the session to log them in
    session['user_id'] = user.id

    # Redirect to a protected page, e.g., a user profile or dashboard
    return redirect(url_for('main.profile'))
