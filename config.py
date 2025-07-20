import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

class Config:
    """Base configuration."""
    SECRET_KEY = os.environ.get('SECRET_KEY', 'a_default_secret_key')
    DATABASE_URL = os.environ.get('DATABASE_URL')
    if not DATABASE_URL:
        raise ValueError("No DATABASE_URL set for the database connection")

    # OIDC SSO Settings
    OIDC_CLIENT_ID = os.environ.get('OIDC_CLIENT_ID')
    OIDC_CLIENT_SECRET = os.environ.get('OIDC_CLIENT_SECRET')
    OIDC_ISSUER_URL = os.environ.get('OIDC_ISSUER_URL')
