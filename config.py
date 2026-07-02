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
    #
    # ping_interval makes the server send a WebSocket PING control frame every
    # 25s. Without it, an idle chat socket carries no traffic between messages,
    # so a reverse proxy / ingress / NAT (nginx default proxy_read_timeout is
    # 60s) silently drops the connection. The browser often never sees a close
    # frame, so the socket goes half-open: the client still thinks it's
    # connected and keeps sending frames into the void — the message is saved
    # nowhere and simply disappears, with no error, until a hard refresh builds
    # a fresh socket. The periodic ping keeps the connection warm through the
    # proxy and, if the peer really is gone, tears the socket down so the ws
    # extension reconnects (and resubscribes) instead of silently failing.
    # 25s stays comfortably under common 60s idle timeouts. Control frames are
    # invisible to the application/HTMX layer, so no client changes are needed.
    SOCK_SERVER_OPTIONS = {"subprotocols": ["d8_sec"], "ping_interval": 25}

    # Session cookie hardening. The primary local dev workflow runs through k3s
    # at https://d8-chat.local, so SECURE=True is fine. If you need to run the
    # app over plain HTTP (e.g. `python3 run.py` direct), override
    # SESSION_COOKIE_SECURE=False locally.
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    # The CSRF token is rendered into each page once (meta tag + hidden form
    # fields) and reused by every HTMX POST, reaction, and file upload for the
    # life of that page. Flask-WTF defaults WTF_CSRF_TIME_LIMIT to 3600s, so a
    # tab left open longer than an hour starts getting "CSRF token has expired"
    # 400s until a hard refresh. Disabling the time limit keeps the token valid
    # for the session lifetime — it's still bound to the session and SECRET_KEY,
    # so it stays CSRF-safe and is invalidated whenever the session is.
    WTF_CSRF_TIME_LIMIT = None

    # Canonical base URL used when generating links that leave the request
    # cycle (password reset emails, OIDC redirect_uri, push notifications). Set
    # this in production; if unset we fall back to url_for(_external=True),
    # which trusts the request Host header and is vulnerable to host-header
    # injection. Should not include a trailing slash, e.g. "https://chat.example.com".
    PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL")

    # Path to a Firebase service-account JSON file. When set, the push
    # service initializes firebase-admin and dispatches FCM notifications to
    # offline users' devices. When unset, push is disabled — self-hosters
    # who don't bring their own Firebase project still get a working chat.
    FIREBASE_CREDENTIALS_PATH = os.environ.get("FIREBASE_CREDENTIALS_PATH")

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
