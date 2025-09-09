import re
import markdown

from config import Config
import bleach
import emoji
from flask import Flask, g
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

    # --- Register custom template filter for Markdown ---
    @app.template_filter("markdown")
    def markdown_filter(content):
        """
        Converts Markdown content to sanitized HTML with syntax highlighting.
        This uses a two-pass approach to safely handle code blocks and prevent
        double-escaping issues with the HTML sanitizer.
        """
        # Emoji conversion happens first
        content_with_emojis = emoji.emojize(content, language="alias")

        # Define the tags and attributes that we will allow in the final HTML
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
            "a": ["href", "rel", "target"],
        }

        # --- Pass 1: Extract and process code blocks separately ---
        code_blocks = []

        def extract_and_process_code_block(m):
            # The full match (e.g., ```python...```) is m.group(0)
            # Process this block with Markdown to get syntax highlighting and escaping
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
            # Store the safe, processed HTML
            code_blocks.append(block_html)
            # Return a placeholder that contains NO special markdown characters
            return f"D8CHATCODEBLOCKPLACEHOLDER{len(code_blocks)-1}"

        # Use regex to find all fenced code blocks and replace them with placeholders
        content_without_code = re.sub(
            r"(?s)(```.*?```|~~~.*?~~~)",
            extract_and_process_code_block,
            content_with_emojis,
        )

        # Pre-process the remaining content to escape lone ">" characters
        # that would otherwise be incorrectly interpreted as empty blockquotes.
        # This looks for any line that *only* contains a ">".
        content_without_code = re.sub(
            r"^(\s*)>(\s*)$", r"\1&gt;\2", content_without_code, flags=re.MULTILINE
        )

        # --- Pass 2: Process the main content (without code blocks) ---
        # Process the remaining markdown, but disable codehilite for this pass.
        main_html = markdown.markdown(
            content_without_code, extensions=["extra", "pymdownx.tilde", "nl2br"]
        )

        # Linkify and then sanitize the main content.
        def set_link_attrs(attrs, new=False):
            attrs[(None, "target")] = "_blank"
            attrs[(None, "rel")] = "noopener noreferrer"
            return attrs

        linkified_html = bleach.linkify(
            main_html, callbacks=[set_link_attrs], skip_tags=["pre"]
        )

        safe_html = bleach.clean(
            linkified_html, tags=allowed_tags, attributes=allowed_attrs
        )

        # --- Final Step: Re-insert the processed code blocks ---
        for i, block_html in enumerate(code_blocks):
            # Use the new, safe placeholder for replacement
            placeholder = f"D8CHATCODEBLOCKPLACEHOLDER{i}"

            # Markdown sometimes wraps standalone placeholders in <p> tags, so we must replace that.
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
        # Use re.escape to handle special characters in the query
        # Use re.IGNORECASE for case-insensitive matching
        highlighted_text = re.sub(
            f"({re.escape(query)})",
            r"<mark>\1</mark>",
            str(text),
            flags=re.IGNORECASE,
        )
        return Markup(highlighted_text)

    # Import blueprints
    from .routes import main_bp, admin_bp
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
