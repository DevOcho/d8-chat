from flask import Flask
from flask_sock import Sock
from config import Config
from .models import initialize_db
from .sso import init_sso

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

    # Import and register blueprints
    from .routes import main_bp, admin_bp
    app.register_blueprint(main_bp)
    app.register_blueprint(admin_bp, url_prefix='/admin')
    #app.register_blueprint(chat_ws)

    return app
