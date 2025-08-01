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

    # --- Register custom template filter for SANITIZING HTML ---
    @app.template_filter('sanitize_html')
    def sanitize_html_filter(content):
        """
        Sanitizes HTML content from the TipTap editor to prevent XSS.
        """
        # Define allowed tags and attributes based on what TipTap extensions we use.
        # This is a critical security step.
        allowed_tags = [
            'p', 'strong', 'em', 'ul', 'ol', 'li', 'br',
            'pre', 'code', 'span', 'div' # For code blocks and syntax highlighting
        ]
        allowed_attrs = {
            '*': ['class'],  # Allow 'class' for syntax highlighting
            'code': ['class'],
        }

        # Sanitize the HTML
        safe_html = bleach.clean(content, tags=allowed_tags, attributes=allowed_attrs)

        return Markup(safe_html)

    # Import and register blueprints
    from .routes import main_bp, admin_bp
    app.register_blueprint(main_bp)
    app.register_blueprint(admin_bp, url_prefix='/admin')

    return app
