# app/__init__.py

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
from .models import User, initialize_db
from .services import minio_service
from .sso import init_sso

sock = Sock()  # Create a Sock instance

# Flask Login =================================================================
login_manager = LoginManager()


@login_manager.user_loader
def load_user(user_id):
    return User.get_or_none(User.id == user_id)


def create_app(config_class=Config):
    """
    Creates and configures the Flask application.
    """
    app = Flask(__name__, static_folder="static", static_url_path="")

    # Load configuration from the config object
    app.config.from_object(config_class)

    # Conditionally apply ProxyFix based on separate counts for different headers.
    try:
        x_for = int(os.environ.get("X_FOR_COUNT", 0))
        x_proto = int(os.environ.get("X_PROTO_COUNT", 0))
    except ValueError:
        x_for = 0
        x_proto = 0

    if x_for > 0 or x_proto > 0:
        # We only pass parameters if their count is greater than 0
        app.wsgi_app = ProxyFix(
            app.wsgi_app, x_for=x_for, x_proto=x_proto, x_host=0, x_port=0, x_prefix=0
        )
        app.logger.info(f"Applying ProxyFix with x_for={x_for}, x_proto={x_proto}.")

    # Ensure SECRET_KEY is set for session management
    if not app.config["SECRET_KEY"]:
        raise ValueError("A SECRET_KEY must be set in the configuration.")

    initialize_db(app)

    # Initialize Minio Client
    minio_service.init_app(app)

    # Initialize SSO, Flask Login, and Websockets
    init_sso(app)
    login_manager.init_app(app)
    sock.init_app(app)  # Initialize Sock with the app

    # --- Valkey/Redis Pub/Sub Setup ---
    chat_manager.initialize(app)

    # The listener must run in a background thread so it doesn't block the web server.
    if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":

        def run_listener():
            # The thread needs its own application context to access config, etc.
            with app.app_context():
                chat_manager.listen_for_messages()

        # daemon=True ensures the thread will exit when the main app process exits
        listener_thread = threading.Thread(target=run_listener, daemon=True)
        listener_thread.start()

    # --- Register custom template filter for emoji-only messages ---
    @app.template_filter("is_jumboable")
    def is_jumboable_filter(text):
        """
        Checks if a string consists of 1 to 3 emojis and nothing else
        (besides whitespace).
        """
        if not text:
            return False

        # First, convert any shortcodes (like :sob:) to unicode emojis.
        emojized_text = emoji.emojize(text, language="alias")
        stripped_text = emojized_text.strip()
        if not stripped_text:
            return False

        # Now, perform the original checks on the fully-converted string.
        text_without_emojis = emoji.replace_emoji(stripped_text, replace="")
        if text_without_emojis.strip():
            return False

        count = emoji.emoji_count(stripped_text)
        return 1 <= count <= 3

    # --- Register custom template filter for Markdown ---
    @app.template_filter("markdown")
    def markdown_filter(content, context="display"):
        """
        Converts Markdown content to sanitized HTML using a robust multi-stage pipeline.

        This filter is designed to safely render user-generated content by separating
        custom HTML generation (like mentions and channel links) from the main Markdown
        parsing and sanitization process. This is achieved using placeholders.

        The pipeline is as follows:
        1.  Convert emoji shortcodes (e.g., :smile:) to Unicode characters.
        2.  Find all custom patterns (@mentions, #channels), generate their final
            HTML link tags, store them in lists, and replace them in the source
            text with unique, safe placeholders (e.g., D8CHATMENTIONPLACEHOLDER0).
        3.  Escape single-hashtag headers to reserve them for channel/hashtag links.
        4.  Extract and process fenced code blocks, replacing them with placeholders.
        5.  Process the remaining "safe" Markdown (which now only contains basic
            formatting and placeholders) into HTML.
        6.  Sanitize the resulting HTML with Bleach to prevent XSS attacks. The
            placeholders are plain text and will pass through the sanitizer untouched.
        7.  Re-insert the pre-generated, safe HTML for mentions, channels, and
            code blocks back into the sanitized HTML, replacing the placeholders.
        8.  Return the final, safe-to-render HTML Markup object.
        """
        # --- Stage 0: Setup placeholder lists ---
        # These lists will hold the final HTML for our custom elements.
        mention_links = []
        channel_links = []
        code_blocks = []

        # --- Stage 1: Initial Emoji Conversion ---
        # Convert shortcodes like :smile: to their Unicode equivalents (e.g., 😄)
        # before any regex parsing begins.
        content_with_emojis = emoji.emojize(content, language="alias")

        # --- Stage 2: Extract & Replace Mentions with Placeholders ---
        mention_pattern = r"@(\w+)"
        usernames = set(re.findall(mention_pattern, content_with_emojis))
        special_mentions = {"here", "channel"}
        user_mentions_to_find = list(usernames - special_mentions)
        user_map = {}
        if user_mentions_to_find:
            from .models import User  # Late import to prevent circular dependency

            users = User.select().where(User.username.in_(user_mentions_to_find))
            user_map = {u.username: u for u in users}

        def extract_mention(match):
            username = match.group(1)
            # Generate the final HTML for the mention link.
            if username in special_mentions:
                link_html = f'<strong class="mention-special">@{username}</strong>'
            elif username in user_map:
                user = user_map[username]
                dm_url = url_for("dms.get_dm_chat", other_user_id=user.id)
                link_html = f'<a href="#" class="mention-link" hx-get="{dm_url}" hx-target="#chat-messages-container">@{username}</a>'
            else:
                return match.group(0)  # Not a valid mention, leave it as is.

            # Store the final HTML and return a placeholder.
            mention_links.append(link_html)
            return f"D8CHATMENTIONPLACEHOLDER{len(mention_links) - 1}"

        content_with_mention_placeholders = re.sub(
            mention_pattern, extract_mention, content_with_emojis
        )

        # --- Stage 3: Pre-process to "defuse" H1-style Markdown headers ---
        # This prevents lines like "# header" from becoming <h1>, reserving the single '#'
        # for our channel/hashtag link logic.
        def escape_h1_headers(text):
            lines = text.split("\n")
            processed_lines = [
                "\\" + line
                if line.strip().startswith("# ") and not line.strip().startswith("##")
                else line
                for line in lines
            ]
            return "\n".join(processed_lines)

        content_preprocessed = escape_h1_headers(content_with_mention_placeholders)

        # --- Stage 4: Extract & Replace Channels/Hashtags with Placeholders ---
        channel_pattern = r"(?<!#)#([a-zA-Z0-9_-]+)"
        potential_channel_names = set(re.findall(channel_pattern, content_preprocessed))
        channel_map = {}
        if potential_channel_names:
            from .models import Channel  # Late import

            channels = Channel.select().where(
                Channel.name.in_(list(potential_channel_names))
            )
            channel_map = {c.name: c for c in channels}

        def extract_channel_tag(match):
            tag_name = match.group(1)
            # Generate the final HTML for the channel or hashtag link.
            if tag_name in channel_map:
                channel = channel_map[tag_name]
                channel_url = url_for(
                    "channels.get_channel_chat", channel_id=channel.id
                )
                link_html = f'<a href="#" class="channel-link" hx-get="{channel_url}" hx-target="#chat-messages-container">#{tag_name}</a>'
            else:
                search_url = url_for("search.search", q=f"#{tag_name}")
                link_html = f'<a href="#" class="hashtag-link" hx-get="{search_url}" hx-target="#search-results-overlay" hx-swap="innerHTML">#{tag_name}</a>'

            # Store the final HTML and return a placeholder.
            channel_links.append(link_html)
            return f"D8CHATCHANNELPLACEHOLDER{len(channel_links) - 1}"

        content_with_all_placeholders = re.sub(
            channel_pattern, extract_channel_tag, content_preprocessed
        )

        # --- Stage 5: Extract & Replace Code Blocks ---
        # This is done to protect the syntax-highlighted HTML from the sanitizer.
        def extract_and_process_code_block(m):
            block_html = markdown.markdown(
                m.group(0),
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

        content_without_code = re.sub(
            r"(?s)(```.*?```|~~~.*?~~~)",
            extract_and_process_code_block,
            content_with_all_placeholders,
        )

        # --- Stage 6: Process Main Markdown, Linkify, & Sanitize ---
        main_html = markdown.markdown(
            content_without_code, extensions=["extra", "pymdownx.tilde", "nl2br"]
        )

        def set_link_attrs(attrs, new=False):
            attrs[(None, "target")] = "_blank"
            attrs[(None, "rel")] = "noopener noreferrer"
            return attrs

        linkified_html = bleach.linkify(
            main_html, callbacks=[set_link_attrs], skip_tags=["pre", "code"]
        )

        # Define allowed tags and attributes for sanitization. Note that HTMX attributes
        # are not needed here because they are safely stored in our placeholder HTML.
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

        safe_html = bleach.clean(
            linkified_html, tags=allowed_tags, attributes=allowed_attrs
        )

        # --- Final Stage: Re-insert all placeholders ---
        # Now we replace the safe placeholders in our sanitized HTML with the full,
        # pre-generated HTML snippets we stored earlier.
        final_html = safe_html
        for i, block_html in enumerate(code_blocks):
            # Markdown can sometimes wrap a lone placeholder in a <p> tag, so we handle both cases.
            final_html = final_html.replace(
                f"<p>D8CHATCODEBLOCKPLACEHOLDER{i}</p>", block_html
            ).replace(f"D8CHATCODEBLOCKPLACEHOLDER{i}", block_html)
        for i, link_html in enumerate(channel_links):
            final_html = final_html.replace(f"D8CHATCHANNELPLACEHOLDER{i}", link_html)
        for i, link_html in enumerate(mention_links):
            final_html = final_html.replace(f"D8CHATMENTIONPLACEHOLDER{i}", link_html)

        # Return as a Markup object to prevent Jinja from re-escaping our HTML.
        return Markup(final_html)

    # --- Register the helper function for rendering polls ---
    @app.context_processor
    def inject_poll_context_helper():
        """Makes the get_poll_context function available to all templates."""
        # We import here to avoid circular dependencies at startup.
        from .blueprints.polls import get_poll_context

        return dict(get_poll_context=get_poll_context)

    # --- Register custom template filter for just emojis ---
    @app.template_filter("emojize")
    def emojize_filter(content):
        """Converts emoji shortcodes in a string to their unicode characters."""
        return emoji.emojize(content, language="alias")

    # --- Register custom template filter for highlighting search terms ---
    @app.template_filter("highlight")
    def highlight_filter(text, query):
        """Wraps occurrences of the query in the text with <mark> tags."""
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
        """Converts bytes to a human-readable format (KB, MB, GB)."""
        if not size:
            return "0 B"
        power = 1024
        n = 0
        power_labels = {0: "", 1: "K", 2: "M", 3: "G", 4: "T"}
        while size >= power and n < len(power_labels) - 1:
            size /= power
            n += 1
        return f"{size:.2f} {power_labels[n]}B"

    # Import blueprints
    from .blueprints.activity import activity_bp
    from .blueprints.admin import admin_bp
    from .blueprints.auth import auth_bp
    from .blueprints.channels import channels_bp
    from .blueprints.dms import dms_bp
    from .blueprints.files import files_bp
    from .blueprints.messages import messages_bp
    from .blueprints.polls import polls_bp
    from .blueprints.profile import profile_bp
    from .blueprints.search import search_bp
    from .routes import main_bp

    # Register blueprints
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

    return app
