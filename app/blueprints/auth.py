# app/blueprints/auth.py
import secrets

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import login_user, logout_user

from app import limiter, login_username_key
from app.auth_tokens import make_password_reset_token, verify_password_reset_token
from app.models import User
from app.sso import oauth

auth_bp = Blueprint("auth", __name__)


# Minimum length for self-service password resets. Doesn't apply to
# admin-set passwords (those go through admin.py and are deliberately
# under-validated for ops-driven first-time provisioning).
MIN_PASSWORD_LENGTH = 12


@auth_bp.route("/")
def index():
    return render_template("index.html")


@auth_bp.route("/login")
def login_page():
    # This route is deprecated in favor of the index page form
    return redirect(url_for("auth.index"))


@auth_bp.route("/login", methods=["POST"])
@limiter.limit("5 per minute; 50 per hour")
@limiter.limit("10 per minute; 50 per hour", key_func=login_username_key)
def login():
    """Handles username/password login form submission."""
    username = request.form.get("username")
    password = request.form.get("password")

    user = User.get_or_none((User.username == username) | (User.email == username))

    # Treat deactivated accounts the same as wrong credentials so we don't leak
    # account-status info in the login error message.
    if user and user.is_active and user.check_password(password):
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


# --- Password reset ---------------------------------------------------------


@auth_bp.route("/forgot-password", methods=["GET"])
def forgot_password_form():
    """Render the form that asks for an email."""
    return render_template("forgot_password.html")


@auth_bp.route("/forgot-password", methods=["POST"])
@limiter.limit("3 per minute; 20 per hour")
def forgot_password():
    """
    Issue a password reset link for the supplied email.

    Always returns the same "if it exists" message so an attacker can't enumerate
    accounts. The actual reset URL is logged via ``current_app.logger.info`` —
    real SMTP delivery is a follow-up; for now the operator forwards the link.
    """
    email = (request.form.get("email") or "").strip().lower()

    if email:
        user = User.get_or_none(User.email == email)
        if user and user.is_active and user.password_hash:
            token = make_password_reset_token(current_app.config["SECRET_KEY"], user)
            reset_url = url_for("auth.reset_password", token=token, _external=True)
            current_app.logger.info(
                f"Password reset link for user {user.id} ({user.email}): {reset_url}"
            )
            # TODO: send the reset URL via SMTP/Mailgun/SES once email is wired up.

    flash(
        "If that email exists, we've sent a link to reset your password. "
        "The link expires in 30 minutes.",
        "info",
    )
    return redirect(url_for("auth.forgot_password_form"))


@auth_bp.route("/reset-password/<token>", methods=["GET"])
def reset_password_form(token: str):
    """Render the new-password form if the token is still valid."""
    user = verify_password_reset_token(current_app.config["SECRET_KEY"], token)
    if user is None:
        return render_template(
            "reset_password.html",
            token=None,
            error=("This reset link is invalid or has expired. Request a new one."),
        ), 400
    return render_template("reset_password.html", token=token, error=None)


@auth_bp.route("/reset-password/<token>", methods=["POST"])
@limiter.limit("5 per minute; 20 per hour")
def reset_password(token: str):
    """Validate token + new password, then update the user's hash."""
    user = verify_password_reset_token(current_app.config["SECRET_KEY"], token)
    if user is None:
        return render_template(
            "reset_password.html",
            token=None,
            error=("This reset link is invalid or has expired. Request a new one."),
        ), 400

    password = request.form.get("password") or ""
    confirm = request.form.get("password_confirm") or ""

    if password != confirm:
        return render_template(
            "reset_password.html",
            token=token,
            error="The two passwords don't match.",
        ), 400
    if len(password) < MIN_PASSWORD_LENGTH:
        return render_template(
            "reset_password.html",
            token=token,
            error=f"Password must be at least {MIN_PASSWORD_LENGTH} characters.",
        ), 400

    user.set_password(password)
    user.save()
    current_app.logger.info(f"Password reset completed for user {user.id}")
    flash(
        "Password reset successful. You can now log in with your new password.",
        "success",
    )
    return redirect(url_for("auth.index"))
