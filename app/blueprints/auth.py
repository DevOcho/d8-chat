# app/blueprints/auth.py
import secrets

from flask import (
    Blueprint,
    current_app,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import login_user, logout_user

from app.models import User
from app.sso import oauth

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/")
def index():
    return render_template("index.html")


@auth_bp.route("/login")
def login_page():
    # This route is deprecated in favor of the index page form
    return redirect(url_for("auth.index"))


@auth_bp.route("/login", methods=["POST"])
def login():
    """Handles username/password login form submission."""
    username = request.form.get("username")
    password = request.form.get("password")

    user = User.get_or_none((User.username == username) | (User.email == username))

    if user and user.check_password(password):
        login_user(user)
        session["user_id"] = user.id
        return redirect(url_for("main.chat_interface"))

    return redirect(url_for("auth.index", error="Invalid username or password."))


@auth_bp.route("/sso-login")
def sso_login():
    """Redirects to the SSO provider for login."""
    redirect_uri = url_for("auth.authorize", _external=True)
    nonce = secrets.token_urlsafe(16)
    session["nonce"] = nonce
    current_app.logger.info(
        f"Redirecting to Authentik with redirect_uri: {redirect_uri}"
    )
    return oauth.authentik.authorize_redirect(redirect_uri, nonce=nonce)


@auth_bp.route("/auth")
def authorize():
    """The callback route for the SSO provider."""
    from app.sso import handle_auth_callback

    return handle_auth_callback()


@auth_bp.route("/logout")
def logout():
    """Logs the user out by clearing the session."""
    logout_user()
    session.clear()
    return redirect(url_for("auth.index"))
