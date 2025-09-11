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

        # [THE FIX] First, convert any shortcodes (like :sob:) to unicode emojis.
        emojized_text = emoji.emojize(text, language="alias")
        stripped_text = emojized_text.strip()
        if not stripped_text:
            return False

        # Now, perform the original checks on the fully-converted string.
        text_without_emojis = emoji.replace_emoji(stripped_text, replace='')
        if text_without_emojis.strip():
            return False
        
        count = emoji.emoji_count(stripped_text)
        return 1 <= count <= 3

    # --- Register custom template filter for Markdown ---
    @app.template_filter("markdown")
    def markdown_filter(content):
        """
        Converts Markdown content to sanitized HTML with syntax highlighting,
        and linkifies @mentions to user profiles.
        """
        # Emoji conversion happens first
        content_with_emojis = emoji.emojize(content, language="alias")

        # --- [NEW] Mention Linkification Logic ---
        # 1. Find all potential @username patterns in the text.
        mention_pattern = r"@(\w+)"
        usernames = set(re.findall(mention_pattern, content_with_emojis))

        # 2. Separate special mentions from user mentions and query the database once.
        special_mentions = {'here', 'channel'}
        user_mentions_to_find = list(usernames - special_mentions)
        user_map = {}
        if user_mentions_to_find:
            users = User.select().where(User.username.in_(user_mentions_to_find))
            user_map = {u.username: u for u in users}

        # 3. Define a replacement function to be used by re.sub().
        def replace_mention(match):
            username = match.group(1)
            if username in special_mentions:
                # Style special mentions like @here and @channel differently.
                return f'<strong class="mention-special">@{username}</strong>'

            user = user_map.get(username)
            if user:
                # This is a valid user. Create a clickable HTMX link to their profile.
                dm_url = url_for('dms.get_dm_chat', other_user_id=user.id)
                return f'<a href="#" class="mention-link" hx-get="{dm_url}" hx-target="#chat-messages-container">@{username}</a>'
            else:
                # This is not a valid user, so return the original text (e.g., "@unknownuser").
                return match.group(0)

        # 4. Perform the substitution on the content.
        content_with_mentions = re.sub(mention_pattern, replace_mention, content_with_emojis)
        # --- [END OF NEW LOGIC] ---

        # Define the tags and attributes that we will allow in the final HTML
        allowed_tags = [
            "p", "br", "strong", "em", "del", "sub", "sup", "ul", "ol", "li",
            "blockquote", "pre", "code", "span", "div", "a", "h1", "h2", "h3",
            "h4", "h5", "h6", "table", "thead", "tbody", "tr", "th", "td",
        ]
        allowed_attrs = {
            "*": ["class"],
            # [MODIFIED] Add hx-get and hx-target to the list of allowed attributes for <a> tags.
            "a": ["href", "rel", "target", "hx-get", "hx-target"],
        }

        # --- Pass 1: Extract and process code blocks separately ---
        code_blocks = []
        def extract_and_process_code_block(m):
            block_html = markdown.markdown(
                m.group(0),
                extensions=["extra", "codehilite", "pymdownx.tilde"],
                extension_configs={
                    "codehilite": { "css_class": "codehilite", "guess_lang": False, "linenums": False, }
                },
            )
            code_blocks.append(block_html)
            return f"D8CHATCODEBLOCKPLACEHOLDER{len(code_blocks)-1}"

        # Use content_with_mentions which has our new links
        content_without_code = re.sub(
            r"(?s)(```.*?```|~~~.*?~~~)",
            extract_and_process_code_block,
            content_with_mentions,
        )

        content_without_code = re.sub(
            r"^(\s*)>(\s*)$", r"\1&gt;\2", content_without_code, flags=re.MULTILINE
        )

        # --- Pass 2: Process the main content (without code blocks) ---
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

        safe_html = bleach.clean(
            linkified_html, tags=allowed_tags, attributes=allowed_attrs
        )

        # --- Final Step: Re-insert the processed code blocks ---
        for i, block_html in enumerate(code_blocks):
            placeholder = f"D8CHATCODEBLOCKPLACEHOLDER{i}"
            placeholder_with_p_tags = f"<p>{placeholder}</p>"
            if placeholder_with_p_tags in safe_html:
                safe_html = safe_html.replace(placeholder_with_p_tags, block_html)
            else:
                safe_html = safe_html.replace(placeholder, block_html)

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
        power_labels = {0: '', 1: 'K', 2: 'M', 3: 'G', 4: 'T'}
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
