from flask import Flask
from flask_sock import Sock
from config import Config
from .models import initialize_db
from .sso import init_sso
import markdown
from markupsafe import Markup
import bleach

sock = Sock() # Create a Sock instance

def create_app(config_class=Config):
    """
    Creates and configures the Flask application.
    """
    app = Flask(__name__)

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
        fenced_code = github style ``` code blocks ```
        codehilite = syntax highlighting
        """
        # 1. Define what HTML is allowed after conversion
        allowed_tags = [
            'p', 'pre', 'code', 'blockquote', 'strong', 'em', 'h1', 'h2', 'h3',
            'ul', 'ol', 'li', 'br', 'span', 'div'
        ]
        allowed_attrs = {
            '*': ['class'], # Allow the 'class' attribute on any tag
        }

        # 2. Convert Markdown to HTML
        html = markdown.markdown(
            content,
            extensions=['fenced_code', 'codehilite'],
            extension_configs={
                'codehilite': {
                    'css_class': 'codehilite',
                    'guess_lang': False,
                    'linenums': False # Optional: set to True to show line numbers
                }
            }
        )

        # 3. Sanitize the HTML to prevent XSS attacks
        safe_html = bleach.clean(html, tags=allowed_tags, attributes=allowed_attrs)

        # Mark the output as safe to prevent auto-escaping
        return Markup(safe_html)

    # Import and register blueprints
    from .routes import main_bp, admin_bp
    app.register_blueprint(main_bp)
    app.register_blueprint(admin_bp, url_prefix='/admin')

    return app
