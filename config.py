import os

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


class Config:
    """Base configuration."""

    SECRET_KEY = os.environ.get("SECRET_KEY")
    if not SECRET_KEY:
        raise ValueError(
            "SECRET_KEY environment variable must be set. "
            "Generate one with: python -c 'import secrets; print(secrets.token_urlsafe(48))'"
        )
    if len(SECRET_KEY) < 32:
        raise ValueError(
            "SECRET_KEY must be at least 32 characters. "
            "Generate one with: python -c 'import secrets; print(secrets.token_urlsafe(48))'"
        )

    # Check if a full URI is provided. If not, build it from components.
    DATABASE_URI = os.environ.get("DATABASE_URI")
    if not DATABASE_URI:
        postgres_user = os.environ.get("POSTGRES_USER")
        postgres_password = os.environ.get("POSTGRES_PASSWORD")
        postgres_host = os.environ.get("POSTGRES_HOST")
        postgres_db = os.environ.get("POSTGRES_DB")

        if all([postgres_user, postgres_password, postgres_host, postgres_db]):
            DATABASE_URI = f"postgresql://{postgres_user}:{postgres_password}@{postgres_host}:5432/{postgres_db}"
        else:
            raise ValueError(
                "Database connection not configured. Set either DATABASE_URI or all POSTGRES_* variables."
            )

    # OIDC SSO Settings
    OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID")
    OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET")
    OIDC_ISSUER_URL = os.environ.get("OIDC_ISSUER_URL")

    # Minio/S3 Settings
    MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "localhost:9000")
    MINIO_ACCESS_KEY = os.environ.get("MINIO_ROOT_USER")
    MINIO_SECRET_KEY = os.environ.get("MINIO_ROOT_PASSWORD")
    MINIO_BUCKET_NAME = os.environ.get("MINIO_BUCKET_NAME", "d8chat")
    MINIO_SECURE = os.environ.get("MINIO_SECURE", "False").lower() == "true"
    MINIO_PUBLIC_URL = os.environ.get("MINIO_PUBLIC_URL")

    # Valkey Config for message broker
    VALKEY_URL = os.environ.get("VALKEY_URL")

    # Shared secret for the service-to-service /api/v1/internal/notify
    # endpoint. The helpdesk service (and any other internal caller) sends
    # this value in the X-Internal-Key header. If unset, the endpoint
    # rejects every request with 401.
    INTERNAL_NOTIFY_KEY = os.environ.get("INTERNAL_NOTIFY_KEY")

    # flask-sock passes these to simple_websocket.Server. Listing "d8_sec" as a
    # known subprotocol lets the API WebSocket route negotiate it back to the
    # client when the client offers it via the Sec-WebSocket-Protocol header.
    # The /ws/chat web route never offers a subprotocol, so this is a no-op
    # there.
    SOCK_SERVER_OPTIONS = {"subprotocols": ["d8_sec"]}

    # Session cookie hardening. The primary local dev workflow runs through k3s
    # at https://d8-chat.local, so SECURE=True is fine. If you need to run the
    # app over plain HTTP (e.g. `python3 run.py` direct), override
    # SESSION_COOKIE_SECURE=False locally.
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    # Branding shown to mobile clients via /api/v1/app-config. Override per
    # deployment without a code change.
    BRAND_SERVER_NAME = os.environ.get("BRAND_SERVER_NAME", "DevOcho")
    BRAND_LOGO_URL = os.environ.get("BRAND_LOGO_URL")
    BRAND_PRIMARY_COLOR = os.environ.get("BRAND_PRIMARY_COLOR", "#ec729c")
    BRAND_SSO_PROVIDER_NAME = os.environ.get(
        "BRAND_SSO_PROVIDER_NAME", "Sign in with SSO"
    )


class TestConfig(Config):
    """Configuration for testing."""

    TESTING = True
    # Use an in-memory SQLite database for tests to keep them fast and isolated
    DATABASE_URI = "sqlite:///:memory:"
    # Disable CSRF protection in testing forms
    WTF_CSRF_ENABLED = False
    # Use a dummy secret key for tests (must be ≥32 chars per the validator)
    SECRET_KEY = "test-secret-key-at-least-32-chars-long"
    # Make login easier for tests
    LOGIN_DISABLED = False

    # Provide dummy values for Minio so the app can initialize during tests.
    # The actual service will be mocked in the tests themselves.
    MINIO_ACCESS_KEY = "test-key"
    MINIO_SECRET_KEY = "test-secret"
    MINIO_ENDPOINT = "testhost:9000"
    MINIO_PUBLIC_URL = "http://testhost:9000"

    # Disable rate limiting in tests so requests are never blocked
    RATELIMIT_ENABLED = False
    RATELIMIT_STORAGE_URI = "memory://"

    # Test client uses HTTP, so a Secure-only cookie would never be sent back.
    SESSION_COOKIE_SECURE = False

    # Fixed shared secret for the internal notify endpoint in tests.
    INTERNAL_NOTIFY_KEY = "test-internal-notify-key"
