# app/__init__.py

import re
import markdown

from config import Config
import bleach
import emoji
from flask import Flask, g, url_for
from flask_login import LoginManager
from flask_sock import Sock
from markupsafe import Markup

from .models import initialize_db, User
from .sso import init_sso
from .services import minio_service

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
    def markdown_filter(content):
        """
        Converts Markdown content to sanitized HTML.

        This filter is a multi-stage pipeline designed to safely and correctly
        render user-generated content:
        1.  Converts emoji shortcodes (e.g., :smile:) to Unicode characters.
        2.  Linkifies @mentions to user profiles and special keywords.
        3.  Escapes single-hashtag headers (`# header`) to prevent them from
            becoming `<h1>` tags, reserving `##` and `###` for headers.
        4.  Linkifies #channel-names to their respective channels.
        5.  Separates and processes fenced code blocks for syntax highlighting,
            replacing them with a placeholder to protect them from sanitization.
        6.  Converts the remaining Markdown to HTML.
        7.  Linkifies standard URLs (e.g., google.com).
        8.  Sanitizes the resulting HTML to prevent XSS attacks.
        9.  Re-inserts the syntax-highlighted code blocks.
        10. Returns the final, safe-to-render HTML Markup object.
        """
        # --- Stage 1: Initial Emoji Conversion ---
        # We run this first to ensure that any emoji shortcodes are converted
        # to their Unicode equivalents before any regex parsing begins.
        content_with_emojis = emoji.emojize(content, language="alias")

        # --- Stage 2: Mention Linkification ---
        # Find all potential @username patterns.
        mention_pattern = r"@(\w+)"
        usernames = set(re.findall(mention_pattern, content_with_emojis))

        # Separate special keywords from potential user mentions to avoid unnecessary DB queries.
        special_mentions = {"here", "channel"}
        user_mentions_to_find = list(usernames - special_mentions)
        user_map = {}
        if user_mentions_to_find:
            from .models import User  # Import here to avoid circular dependency

            # Query the database once for all potential usernames found.
            users = User.select().where(User.username.in_(user_mentions_to_find))
            user_map = {u.username: u for u in users}

        # This function is used by re.sub to perform the replacement logic.
        def replace_mention(match):
            username = match.group(1)
            # Handle special mentions with a distinct style.
            if username in special_mentions:
                return f'<strong class="mention-special">@{username}</strong>'

            # Handle valid user mentions by creating an HTMX link.
            user = user_map.get(username)
            if user:
                dm_url = url_for("dms.get_dm_chat", other_user_id=user.id)
                return f'<a href="#" class="mention-link" hx-get="{dm_url}" hx-target="#chat-messages-container">@{username}</a>'
            else:
                # If it's not a special mention or a valid user, return the original text.
                return match.group(0)

        # Perform the substitution on the content.
        content_with_mentions = re.sub(
            mention_pattern, replace_mention, content_with_emojis
        )

        # --- Stage 3: Pre-process to "defuse" H1-style Markdown headers ---
        # The Markdown library aggressively converts any line starting with `# ` into an `<h1>`.
        # We want to reserve this for channel links. This function finds those lines and
        # prepends a backslash, telling the Markdown parser to treat the '#' as a literal character.
        def escape_h1_headers(text):
            lines = text.split("\n")
            processed_lines = []
            for line in lines:
                stripped_line = line.strip()
                # Check for `# ` but ignore `## ` and `### `
                if stripped_line.startswith("# ") and not stripped_line.startswith(
                    "##"
                ):
                    processed_lines.append("\\" + line)
                else:
                    processed_lines.append(line)
            return "\n".join(processed_lines)

        content_preprocessed = escape_h1_headers(content_with_mentions)

        # --- Stage 4: Channel Linkification ---
        # This regex uses a "negative lookbehind" `(?<!#)` to find `#channel-name`
        # but ignore `##header`, ensuring we only match potential channel links.
        channel_pattern = r"(?<!#)#([a-zA-Z0-9_-]+)"
        potential_channel_names = set(re.findall(channel_pattern, content_preprocessed))

        channel_map = {}
        if potential_channel_names:
            from .models import Channel  # Import here to avoid circular dependency

            # Query the database once for all potential channel names.
            channels = Channel.select().where(
                Channel.name.in_(list(potential_channel_names))
            )
            channel_map = {c.name: c for c in channels}

        # This function replaces valid channel tags with HTMX links.
        def replace_channel_tag(match):
            channel_name = match.group(1)
            channel = channel_map.get(channel_name)
            if channel:
                # If the channel exists, create a link to it.
                channel_url = url_for(
                    "channels.get_channel_chat", channel_id=channel.id
                )
                return f'<a href="#" class="channel-link" hx-get="{channel_url}" hx-target="#chat-messages-container">#{channel_name}</a>'
            else:
                # If not a valid channel, return the original text.
                return match.group(0)

        content_with_channels = re.sub(
            channel_pattern, replace_channel_tag, content_preprocessed
        )

        # --- Stage 5: Main Markdown Processing ---
        # Define the HTML tags and attributes we will allow in the final, sanitized output.
        allowed_tags = [
            "p",
            "br",
            "strong",
            "em",
            "del",
            "sub",
            "sup",
            "ul",
            "ol",
            "li",
            "blockquote",
            "pre",
            "code",
            "span",
            "div",
            "a",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "table",
            "thead",
            "tbody",
            "tr",
            "th",
            "td",
        ]
        allowed_attrs = {
            "*": ["class"],
            "a": ["href", "rel", "target", "hx-get", "hx-target"],
        }

        # --- Pass 1: Extract and process code blocks separately ---
        # We do this because the sanitizer (bleach) would strip the syntax highlighting tags.
        code_blocks = []

        def extract_and_process_code_block(m):
            # Process the code block with the 'codehilite' extension.
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
            # Store the processed HTML and return a safe placeholder.
            code_blocks.append(block_html)
            return f"D8CHATCODEBLOCKPLACEHOLDER{len(code_blocks)-1}"

        # Run the extraction on our content that already has mentions and channel links.
        content_without_code = re.sub(
            r"(?s)(```.*?```|~~~.*?~~~)",
            extract_and_process_code_block,
            content_with_channels,
        )

        # Fix for a Markdown library quirk where an empty blockquote line is rendered improperly.
        content_without_code = re.sub(
            r"^(\s*)>(\s*)$", r"\1&gt;\2", content_without_code, flags=re.MULTILINE
        )

        # --- Pass 2: Process the main content (without code blocks) ---
        main_html = markdown.markdown(
            content_without_code, extensions=["extra", "pymdownx.tilde", "nl2br"]
        )

        # A callback for bleach.linkify to add target="_blank" to all URLs.
        def set_link_attrs(attrs, new=False):
            attrs[(None, "target")] = "_blank"
            attrs[(None, "rel")] = "noopener noreferrer"
            return attrs

        # Find raw URLs in the text and turn them into clickable links.
        linkified_html = bleach.linkify(
            main_html, callbacks=[set_link_attrs], skip_tags=["pre", "code"]
        )

        # Sanitize the HTML to remove any potentially malicious tags or attributes.
        safe_html = bleach.clean(
            linkified_html, tags=allowed_tags, attributes=allowed_attrs
        )

        # --- Final Step: Re-insert the processed code blocks ---
        for i, block_html in enumerate(code_blocks):
            placeholder = f"D8CHATCODEBLOCKPLACEHOLDER{i}"
            # Markdown sometimes wraps standalone placeholders in <p> tags, so we must replace that.
            placeholder_with_p_tags = f"<p>{placeholder}</p>"
            if placeholder_with_p_tags in safe_html:
                safe_html = safe_html.replace(placeholder_with_p_tags, block_html)
            else:
                safe_html = safe_html.replace(placeholder, block_html)

        # Return the final HTML wrapped in a Markup object to prevent double-escaping by Jinja2.
        return Markup(safe_html)

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
    from .routes import main_bp
    from .blueprints.admin import admin_bp
    from .blueprints.search import search_bp
    from .blueprints.channels import channels_bp
    from .blueprints.dms import dms_bp
    from .blueprints.files import files_bp
    from .blueprints.activity import activity_bp

    # Register blueprints
    app.register_blueprint(main_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(search_bp)
    app.register_blueprint(channels_bp)
    app.register_blueprint(dms_bp)
    app.register_blueprint(files_bp)
    app.register_blueprint(activity_bp)

    return app
