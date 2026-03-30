# app/__init__.py
"""Application factory and initialization module."""

# pylint: disable=import-error

import datetime
import os
import re
import threading

import bleach
import emoji
import markdown
from flask import Flask, url_for
from flask_login import LoginManager
from flask_sock import Sock
from markupsafe import Markup
from werkzeug.middleware.proxy_fix import ProxyFix

from config import Config

from .chat_manager import chat_manager
from .models import Channel, User, initialize_db
from .services import minio_service
from .sso import init_sso

sock = Sock()  # Create a Sock instance

# Flask Login =================================================================
login_manager = LoginManager()


@login_manager.user_loader
def load_user(user_id):
    """Loads a user by ID for Flask-Login."""
    return User.get_or_none(User.id == user_id)


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

        now = datetime.datetime.now()
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


def create_app(config_class=Config, start_listener=True):
    """
    Creates and configures the Flask application.
    """
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

    if not app.config["SECRET_KEY"]:
        raise ValueError("A SECRET_KEY must be set in the configuration.")

    initialize_db(app)
    minio_service.init_app(app)
    init_sso(app)
    login_manager.init_app(app)
    sock.init_app(app)
    chat_manager.initialize(app)

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

    return app
