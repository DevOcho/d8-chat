import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


class Config:
    """Base configuration."""

    SECRET_KEY = os.environ.get("SECRET_KEY", "a_default_secret_key")
    DATABASE_URI = os.environ.get("DATABASE_URI")
    if not DATABASE_URI:
        raise ValueError("No DATABASE_URI set for the database connection")

    # OIDC SSO Settings
    OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID")
    OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET")
    OIDC_ISSUER_URL = os.environ.get("OIDC_ISSUER_URL")


class TestConfig(Config):
    """Configuration for testing."""

    TESTING = True
    # Use an in-memory SQLite database for tests to keep them fast and isolated
    DATABASE_URI = "sqlite:///:memory:"
    # Disable CSRF protection in testing forms
    WTF_CSRF_ENABLED = False
    # Use a dummy secret key for tests
    SECRET_KEY = "my-test-secret-key"
    # Make login easier for tests
    LOGIN_DISABLED = False
