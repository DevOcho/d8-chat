import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


class Config:
    """Base configuration."""

    SECRET_KEY = os.environ.get("SECRET_KEY", "a_default_secret_key")

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
            raise ValueError("Database connection not configured. Set either DATABASE_URI or all POSTGRES_* variables.")

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

    # Provide dummy values for Minio so the app can initialize during tests.
    # The actual service will be mocked in the tests themselves.
    MINIO_ACCESS_KEY = "test-key"
    MINIO_SECRET_KEY = "test-secret"
    MINIO_ENDPOINT = "testhost:9000"
    MINIO_PUBLIC_URL = "http://testhost:9000"
