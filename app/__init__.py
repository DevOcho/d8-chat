from flask import Flask
from flask_sock import Sock
from config import Config
from .models import initialize_db
from .sso import init_sso
import markdown
from markupsafe import Markup
import bleach
import emoji # Import the new library

sock = Sock() # Create a Sock instance

def create_app(config_class=Config):
    """
    Creates and configures the Flask application.
    """
    app = Flask(__name__, static_folder="static", static_url_path="")

    # Load configuration from the config object
    app.config.from_object(config_class)

    # Ensure SECRET_KEY is set for session management
    if not app.config['SECRET_KEY']:
        raise ValueError("A SECRET_KEY must be set in the configuration.")

    # Initialize SSO
    init_sso(app)
    sock.init_app(app) # Initialize Sock with the app

    # Initialize the database connection
    with app.app_context():
        initialize_db()

    # --- Register custom template filter for Markdown ---
    @app.template_filter('markdown')
    def markdown_filter(content):
        """
        Converts Markdown content to sanitized HTML with syntax highlighting.
        """
        # First, convert emoji shortcodes (e.g., :joy:) into unicode characters.
        # The 'alias' language allows for common shortcodes like :D
        content_with_emojis = emoji.emojize(content, language='alias')

        # Define the tags and attributes that we will allow in the final HTML
        allowed_tags = [
            'p', 'pre', 'code', 'blockquote', 'strong', 'em', 'h1', 'h2', 'h3',
            'ul', 'ol', 'li', 'br', 'span', 'div'
        ]
        allowed_attrs = {
            '*': ['class'],
        }

        # Setup the markdown
        html = markdown.markdown(
            content_with_emojis, # Use the emoji-processed content
            extensions=['extra', 'codehilite', 'nl2br'],
            extension_configs={
                'codehilite': {
                    'css_class': 'codehilite',
                    'guess_lang': False,
                    'linenums': False
                }
            }
        )

        safe_html = bleach.clean(html, tags=allowed_tags, attributes=allowed_attrs)

        return Markup(safe_html)

    # Import and register blueprints
    from .routes import main_bp, admin_bp
    app.register_blueprint(main_bp)
    app.register_blueprint(admin_bp, url_prefix='/admin')

    return app
