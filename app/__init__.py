# app/__init__.py
"""Application factory and initialization module."""

# pylint: disable=import-error

import os
import re
import threading
from urllib.parse import urlparse

import bleach
import emoji
import markdown
from flask import Flask, jsonify, render_template, request, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager
from flask_sock import Sock
from flask_wtf.csrf import CSRFProtect
from markupsafe import Markup
from werkzeug.middleware.proxy_fix import ProxyFix

from config import Config

from .chat_manager import chat_manager
from .models import Channel, User, db, initialize_db, utc_now
from .services import minio_service
from .sso import init_sso

sock = Sock()
login_manager = LoginManager()
csrf = CSRFProtect()
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["300 per minute"],
    swallow_errors=True,  # fail open if storage is unavailable
    headers_enabled=True,
)


def login_username_key():
    """
    Rate-limit key that buckets login attempts by submitted username.

    Lets us cap brute-force attempts against a single account even when an
    attacker rotates source IPs. Reads `username` from JSON or form data and
    normalizes case/whitespace so that case variants share a bucket. Falls
    back to the remote address when no username is present (e.g. a malformed
    request) so the limiter still has a stable key.
    """
    username = None
    if request.is_json:
        data = request.get_json(silent=True) or {}
        username = data.get("username")
    if not username and request.form:
        username = request.form.get("username")
    if username:
        return f"login_user:{username.strip().lower()}"
    return get_remote_address()


@login_manager.user_loader
def load_user(user_id):
    """Loads a user by ID for Flask-Login. Skips deactivated users."""
    return User.get_active_by_id(user_id)


def _process_mentions(text, mention_links):
    """Extracts @mentions, generates HTML, and replaces with placeholders."""
    mention_pattern = r"(?<![^\s(\['\"])@(\w+)"
    usernames = set(re.findall(mention_pattern, text))
    special_mentions = {"here", "channel"}
    user_mentions_to_find = list(usernames - special_mentions)
    user_map = {}
    if user_mentions_to_find:
        users = list(User.select().where(User.username.in_(user_mentions_to_find)))
        user_map = {u.username: u for u in users}

    def extract_mention(match):
        username = match.group(1)
        if username in special_mentions:
            link_html = f'<strong class="mention-special">@{username}</strong>'
        elif username in user_map:
            user_obj = user_map[username]
            dm_url = url_for("dms.get_dm_chat", other_user_id=user_obj.id)
            link_html = (
                f'<a href="#" class="mention-link" hx-get="{dm_url}" '
                f'hx-target="#chat-messages-container">@{username}</a>'
            )
        else:
            return match.group(0)

        mention_links.append(link_html)
        return f"D8CHATMENTIONPLACEHOLDER{len(mention_links) - 1}"

    return re.sub(mention_pattern, extract_mention, text)


def _escape_h1_headers(text):
    """Defuses H1-style Markdown headers to reserve single '#' for channels."""
    lines = text.split("\n")
    processed_lines = []
    for line in lines:
        if line.strip().startswith("# ") and not line.strip().startswith("##"):
            processed_lines.append("\\" + line)
        else:
            processed_lines.append(line)
    return "\n".join(processed_lines)


def _process_channels(text, channel_links):
    """Extracts #channels/hashtags, generates HTML, and replaces with placeholders."""
    channel_pattern = r"(?<![^\s(\['\"])#([a-zA-Z0-9_-]+)"
    potential_channel_names = set(re.findall(channel_pattern, text))
    channel_map = {}
    if potential_channel_names:
        channels = list(
            Channel.select().where(Channel.name.in_(list(potential_channel_names)))
        )
        channel_map = {c.name: c for c in channels}

    def extract_channel_tag(match):
        tag_name = match.group(1)
        if tag_name in channel_map:
            channel_obj = channel_map[tag_name]
            channel_url = url_for(
                "channels.get_channel_chat", channel_id=channel_obj.id
            )
            link_html = (
                f'<a href="#" class="channel-link" hx-get="{channel_url}" '
                f'hx-target="#chat-messages-container">#{tag_name}</a>'
            )
        else:
            search_url = url_for("search.search", q=f"#{tag_name}")
            link_html = (
                f'<a href="#" class="hashtag-link" hx-get="{search_url}" '
                f'hx-target="#search-results-overlay" hx-swap="innerHTML">#{tag_name}</a>'
            )

        channel_links.append(link_html)
        return f"D8CHATCHANNELPLACEHOLDER{len(channel_links) - 1}"

    return re.sub(channel_pattern, extract_channel_tag, text)


def _process_code_blocks(text, code_blocks):
    """Extracts fenced code blocks, processes them, and replaces with placeholders."""

    def extract_and_process_code_block(match):
        block_html = markdown.markdown(
            match.group(0),
            extensions=["extra", "codehilite", "pymdownx.tilde"],
            extension_configs={
                "codehilite": {
                    "css_class": "codehilite",
                    "guess_lang": False,
                    "linenums": False,
                }
            },
        )
        code_blocks.append(block_html)
        return f"D8CHATCODEBLOCKPLACEHOLDER{len(code_blocks) - 1}"

    return re.sub(r"(?s)(```.*?```|~~~.*?~~~)", extract_and_process_code_block, text)


def _sanitize_and_linkify(html_text):
    """Sanitizes HTML and linkifies URLs."""

    def set_link_attrs(attrs, _new=False):
        attrs[(None, "target")] = "_blank"
        attrs[(None, "rel")] = "noopener noreferrer"
        return attrs

    linkified_html = bleach.linkify(
        html_text, callbacks=[set_link_attrs], skip_tags=["pre", "code"]
    )

    allowed_tags = [
        "p",
        "br",
        "strong",
        "em",
        "del",
        "ul",
        "ol",
        "li",
        "blockquote",
        "pre",
        "code",
        "span",
        "div",
        "a",
        "h2",
        "h3",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
    ]
    allowed_attrs = {"*": ["class"], "a": ["href", "rel", "target"]}

    return bleach.clean(linkified_html, tags=allowed_tags, attributes=allowed_attrs)


def _validate_config(app):
    """
    Hard-fail at boot on misconfiguration so a bad deploy can't quietly
    serve traffic with insecure defaults.

    Catches three classes of mistake:
      * required values left at placeholder/dev defaults in production
      * partial configuration (e.g. an OIDC client id without its secret)
      * Flask's debug mode enabled when a real ``FLASK_ENV`` is set
    """
    cfg = app.config

    # SECRET_KEY length is enforced in config.py at import time, but we double-
    # check here in case a caller passed a custom config_class.
    secret = cfg.get("SECRET_KEY") or ""
    if not secret or len(secret) < 32:
        raise RuntimeError(
            "SECRET_KEY must be at least 32 characters. "
            "Generate one with: python -c 'import secrets; print(secrets.token_urlsafe(48))'"
        )
    placeholder_keys = {
        "a_default_secret_key",
        "changeme",
        "changeme_must_be_at_least_32_characters_long",
    }
    if secret in placeholder_keys:
        raise RuntimeError(
            "SECRET_KEY is set to a placeholder value. Generate a real secret."
        )

    if not cfg.get("DATABASE_URI"):
        raise RuntimeError(
            "DATABASE_URI is required. Set DATABASE_URI directly or POSTGRES_* env vars."
        )

    # Skip storage/SSO checks during testing — TestConfig populates dummy
    # values that would otherwise trip the placeholder rules.
    if app.testing:
        return

    if not cfg.get("MINIO_ACCESS_KEY") or not cfg.get("MINIO_SECRET_KEY"):
        raise RuntimeError("MINIO_ROOT_USER and MINIO_ROOT_PASSWORD must both be set.")
    if not cfg.get("MINIO_PUBLIC_URL"):
        raise RuntimeError("MINIO_PUBLIC_URL must be set.")

    # OIDC is optional, but if any of the three fields is set, all three must
    # be set — partial config silently falls back to broken auth.
    oidc_fields = (
        cfg.get("OIDC_CLIENT_ID"),
        cfg.get("OIDC_CLIENT_SECRET"),
        cfg.get("OIDC_ISSUER_URL"),
    )
    if any(oidc_fields) and not all(oidc_fields):
        raise RuntimeError(
            "OIDC config is partial. Set all three of OIDC_CLIENT_ID, "
            "OIDC_CLIENT_SECRET, OIDC_ISSUER_URL — or none."
        )

    if app.debug and os.environ.get("FLASK_ENV", "").lower() in {"production", "prod"}:
        raise RuntimeError(
            "Flask debug mode is enabled but FLASK_ENV=production. Refusing to start."
        )

    if not cfg.get("VALKEY_URL"):
        app.logger.warning(
            "VALKEY_URL is not set; rate limiting and pub/sub will degrade to in-process."
        )


def _build_csp(minio_origin):
    """
    Build the Content-Security-Policy for HTML responses.

    `'unsafe-inline'` is unfortunately required for both scripts and styles
    because the templates use a handful of inline `<script>` blocks (theme
    handler, CSRF/HTMX wiring, login form helper) and ~30 elements with inline
    `style="..."`, plus `onclick`/`onsubmit`/`onchange` attributes. Tightening
    this further means refactoring those out and switching to nonces or hashes;
    tracked as a follow-up enhancement.
    """
    img_src = "'self' data: blob:"
    if minio_origin:
        img_src += f" {minio_origin}"
    directives = [
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline'",
        "style-src 'self' 'unsafe-inline'",
        f"img-src {img_src}",
        "font-src 'self' data:",
        "connect-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "object-src 'none'",
    ]
    return "; ".join(directives)


def _register_security_headers(app):
    """Attach baseline security response headers to every response."""
    minio_public_url = (app.config.get("MINIO_PUBLIC_URL") or "").strip()
    minio_origin = ""
    if minio_public_url:
        parsed = urlparse(minio_public_url)
        if parsed.scheme and parsed.netloc:
            minio_origin = f"{parsed.scheme}://{parsed.netloc}"

    html_csp = _build_csp(minio_origin)
    api_csp = "default-src 'none'; frame-ancestors 'none'"

    @app.after_request
    def _set_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Referrer-Policy", "strict-origin-when-cross-origin"
        )
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        # Only emit HSTS over HTTPS to avoid pinning HTTP-only dev hosts.
        if request.is_secure:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        if request.path.startswith("/api/v1"):
            response.headers.setdefault("Content-Security-Policy", api_csp)
        else:
            response.headers.setdefault("Content-Security-Policy", html_csp)
        return response


def register_template_filters(app):
    """Registers all custom template filters for the application."""

    @app.template_filter("is_jumboable")
    def is_jumboable_filter(text):
        if not text:
            return False

        emojized_text = emoji.emojize(text, language="alias")
        stripped_text = emojized_text.strip()
        if not stripped_text:
            return False

        text_without_emojis = emoji.replace_emoji(stripped_text, replace="")
        if text_without_emojis.strip():
            return False

        count = emoji.emoji_count(stripped_text)
        return 1 <= count <= 3

    @app.template_filter("date_label")
    def date_label_filter(date_time_obj):
        if not date_time_obj:
            return ""

        now = utc_now()
        today = now.date()
        date_obj = date_time_obj.date()

        delta = (today - date_obj).days
        if delta == 0:
            return "Today"
        if delta == 1:
            return "Yesterday"

        day = date_obj.day
        if 4 <= day <= 20 or 24 <= day <= 30:
            suffix = "th"
        else:
            suffix = ["st", "nd", "rd"][day % 10 - 1]

        if date_obj.year == today.year:
            return f"{date_obj.strftime('%B')} {day}{suffix}"
        return f"{date_obj.strftime('%B')} {day}{suffix}, {date_obj.year}"

    @app.template_filter("markdown")
    def markdown_filter(content, _context="display"):
        mention_links = []
        channel_links = []
        code_blocks = []

        content_with_emojis = emoji.emojize(content, language="alias")
        content_with_mentions = _process_mentions(content_with_emojis, mention_links)
        content_preprocessed = _escape_h1_headers(content_with_mentions)
        content_with_channels = _process_channels(content_preprocessed, channel_links)
        content_without_code = _process_code_blocks(content_with_channels, code_blocks)

        main_html = markdown.markdown(
            content_without_code, extensions=["extra", "pymdownx.tilde", "nl2br"]
        )

        safe_html = _sanitize_and_linkify(main_html)

        final_html = safe_html
        for i, block_html in enumerate(code_blocks):
            final_html = final_html.replace(
                f"<p>D8CHATCODEBLOCKPLACEHOLDER{i}</p>", block_html
            ).replace(f"D8CHATCODEBLOCKPLACEHOLDER{i}", block_html)
        for i, link_html in enumerate(channel_links):
            final_html = final_html.replace(f"D8CHATCHANNELPLACEHOLDER{i}", link_html)
        for i, link_html in enumerate(mention_links):
            final_html = final_html.replace(f"D8CHATMENTIONPLACEHOLDER{i}", link_html)

        return Markup(final_html)

    @app.context_processor
    def inject_poll_context_helper():
        # pylint: disable=import-outside-toplevel
        from .blueprints.polls import get_poll_context

        return {"get_poll_context": get_poll_context}

    @app.template_filter("emojize")
    def emojize_filter(content):
        return emoji.emojize(content, language="alias")

    @app.template_filter("highlight")
    def highlight_filter(text, query):
        if not query or not text:
            return text
        highlighted_text = re.sub(
            f"({re.escape(query)})",
            r"<mark>\1</mark>",
            str(text),
            flags=re.IGNORECASE,
        )
        return Markup(highlighted_text)

    @app.template_filter("format_bytes")
    def format_bytes_filter(size):
        if not size:
            return "0 B"
        power = 1024
        n = 0
        power_labels = {0: "", 1: "K", 2: "M", 3: "G", 4: "T"}
        while size >= power and n < len(power_labels) - 1:
            size /= power
            n += 1
        return f"{size:.2f} {power_labels[n]}B"


def register_blueprints(app):
    """Registers all blueprints for the application."""
    # pylint: disable=import-outside-toplevel
    from .blueprints.activity import activity_bp
    from .blueprints.admin import admin_bp
    from .blueprints.api_v1 import api_v1_bp
    from .blueprints.auth import auth_bp
    from .blueprints.channels import channels_bp
    from .blueprints.dms import dms_bp
    from .blueprints.files import files_bp
    from .blueprints.messages import messages_bp
    from .blueprints.polls import polls_bp
    from .blueprints.profile import profile_bp
    from .blueprints.search import search_bp
    from .routes import main_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(auth_bp)
    app.register_blueprint(search_bp)
    app.register_blueprint(channels_bp)
    app.register_blueprint(dms_bp)
    app.register_blueprint(files_bp)
    app.register_blueprint(activity_bp)
    app.register_blueprint(messages_bp)
    app.register_blueprint(polls_bp)
    app.register_blueprint(profile_bp)
    app.register_blueprint(api_v1_bp, url_prefix="/api/v1")


def _init_sentry():
    """
    Wire up Sentry if ``SENTRY_DSN`` is set in the environment.

    Pure no-op when the env var is missing — dev, test, and self-hosted
    deployments without Sentry credentials skip the import entirely. When
    enabled, the Flask integration captures unhandled exceptions automatically
    and the logging integration forwards ``logger.error``/``.exception()``
    calls (which is why we converted print() to logger.exception() earlier).
    """
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return

    import sentry_sdk
    from sentry_sdk.integrations.flask import FlaskIntegration

    sentry_sdk.init(
        dsn=dsn,
        integrations=[FlaskIntegration()],
        # Pull from env so deployments can tune without a code change.
        environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
        release=os.environ.get("SENTRY_RELEASE"),
        # 0.0 disables performance monitoring; opt in via env if you want it.
        traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0")),
        send_default_pii=False,
    )


def create_app(config_class=Config, start_listener=True):
    """
    Creates and configures the Flask application.
    """
    _init_sentry()
    app = Flask(__name__, static_folder="static", static_url_path="")

    app.config.from_object(config_class)

    try:
        x_for = int(os.environ.get("X_FOR_COUNT", 0))
        x_proto = int(os.environ.get("X_PROTO_COUNT", 0))
    except ValueError:
        x_for = 0
        x_proto = 0

    if x_for > 0 or x_proto > 0:
        app.wsgi_app = ProxyFix(
            app.wsgi_app, x_for=x_for, x_proto=x_proto, x_host=0, x_port=0, x_prefix=0
        )
        app.logger.info("Applying ProxyFix with x_for=%s, x_proto=%s.", x_for, x_proto)

    _validate_config(app)

    initialize_db(app)

    @app.before_request
    def _db_connect():
        db.connect(reuse_if_open=True)

    @app.teardown_request
    def _db_close(exc):
        # Skip in tests: closing an in-memory SQLite connection destroys the database
        if not app.testing and not db.is_closed():
            db.close()

    minio_service.init_app(app)
    init_sso(app)
    login_manager.init_app(app)
    sock.init_app(app)
    csrf.init_app(app)
    chat_manager.initialize(app)

    app.config.setdefault(
        "RATELIMIT_STORAGE_URI", app.config.get("VALKEY_URL") or "memory://"
    )
    limiter.init_app(app)

    def _wants_json() -> bool:
        """True when the caller is an API client that should receive JSON."""
        if request.path.startswith("/api/v1"):
            return True
        accept = request.accept_mimetypes
        # If the client explicitly prefers JSON over HTML, honor that.
        return (
            accept.best_match(["application/json", "text/html"]) == "application/json"
        )

    def _error_response(status_code: int, title: str, detail: str):
        if _wants_json():
            return jsonify({"error": title, "detail": detail}), status_code
        return render_template(
            "errors/error.html",
            status_code=status_code,
            title=title,
            detail=detail,
        ), status_code

    @app.errorhandler(404)
    def not_found_handler(e):
        return _error_response(
            404, "Not found", "The page or resource you requested doesn't exist."
        )

    @app.errorhandler(429)
    def rate_limit_handler(e):
        return _error_response(
            429,
            "Rate limit exceeded",
            "You're sending requests too quickly. Try again in a moment.",
        )

    @app.errorhandler(500)
    def server_error_handler(e):
        # Re-raise in debug so the Werkzeug debugger can do its job.
        if app.debug:
            raise e
        app.logger.exception("Unhandled 500 error")
        return _error_response(
            500,
            "Something went wrong",
            "We've logged the error and will look into it.",
        )

    # Add the start_listener check here
    if (
        start_listener
        and not app.testing
        and (not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true")
    ):

        def run_listener():
            with app.app_context():
                chat_manager.listen_for_messages()

        listener_thread = threading.Thread(target=run_listener, daemon=True)
        listener_thread.start()

    register_template_filters(app)
    register_blueprints(app)
    _register_security_headers(app)

    # Mobile/external API uses Bearer tokens, not cookies, so CSRF doesn't
    # apply. Exempt the whole blueprint after registration.
    from .blueprints.api_v1 import api_v1_bp

    csrf.exempt(api_v1_bp)

    return app
