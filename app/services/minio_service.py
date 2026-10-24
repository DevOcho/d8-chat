import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from flask import current_app
from minio import Minio
from minio.error import S3Error

# Check if pytest is running
IS_RUNNING_TESTS = "PYTEST_CURRENT_TEST" in os.environ

# We will now maintain two separate clients
minio_client_internal = None
minio_client_public = None


def init_app(app):
    """
    Initialize two Minio clients: one for internal operations and one for
    generating public-facing presigned URLs.
    """
    global minio_client_internal, minio_client_public

    # Common configuration
    access_key = app.config["MINIO_ACCESS_KEY"]
    secret_key = app.config["MINIO_SECRET_KEY"]
    # secure = app.config["MINIO_SECURE"]
    bucket_name = app.config["MINIO_BUCKET_NAME"]
    # Explicitly set the region to prevent internal lookups
    region = "us-east-1"

    # 1. Configure the INTERNAL client. It respects the MINIO_SECURE flag
    #    because it's for server-to-server communication.
    internal_endpoint = app.config["MINIO_ENDPOINT"]
    internal_secure = app.config["MINIO_SECURE"]
    minio_client_internal = Minio(
        internal_endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=internal_secure,
        region=region,
    )

    # 2. Configure the PUBLIC client. It determines security based on the
    #    MINIO_PUBLIC_URL itself, ignoring the MINIO_SECURE flag.
    # public_endpoint_url = app.config.get("MINIO_PUBLIC_URL", "")
    public_endpoint_url = app.config.get("MINIO_PUBLIC_URL") or ""
    public_endpoint_host = internal_endpoint  # Default to internal
    # Determine if the public URL is secure by checking its scheme.
    public_secure = public_endpoint_url.lower().startswith("https://")

    if public_endpoint_url:
        parsed_url = urlparse(public_endpoint_url)
        if parsed_url.netloc:
            public_endpoint_host = parsed_url.netloc
        else:
            app.logger.warning(
                f"MINIO_PUBLIC_URL ('{public_endpoint_url}') seems to be invalid. "
                "Falling back to internal endpoint for URL generation."
            )

    minio_client_public = Minio(
        public_endpoint_host,
        access_key=access_key,
        secret_key=secret_key,
        secure=public_secure,  # Use the scheme-derived value here
        region=region,
    )

    # During tests, we assume the mocked service works correctly.
    if not IS_RUNNING_TESTS:
        # Bucket existence check is an internal operation
        try:
            found = minio_client_internal.bucket_exists(bucket_name)
            if not found:
                minio_client_internal.make_bucket(bucket_name)
                app.logger.info(f"Minio bucket '{bucket_name}' created.")
            else:
                app.logger.info(f"Minio bucket '{bucket_name}' already exists.")
        except Exception:
            # Catch all exceptions (incl. urllib3 connection errors) so DB scripts don't crash
            app.logger.exception("Could not connect to Minio during initialization")


def upload_file(object_name, file_path, content_type):
    """Uploads a file to the configured Minio bucket using the INTERNAL client."""
    if not minio_client_internal:
        raise Exception("Internal Minio client not initialized.")

    try:
        with open(file_path, "rb") as file_data:
            file_stat = os.stat(file_path)
            minio_client_internal.put_object(
                bucket_name=current_app.config["MINIO_BUCKET_NAME"],
                object_name=object_name,
                data=file_data,
                length=file_stat.st_size,
                content_type=content_type,
            )
        return True
    except S3Error:
        current_app.logger.exception("Error uploading file to Minio")
        return False


# Presigned URLs are signed as of the start of a fixed window rather than
# "now", so every render inside the window yields the same URL and the browser
# can cache images and avatars instead of re-downloading them on every
# conversation switch. A URL is valid for URL_LIFETIME from its window's start,
# so even one generated at the end of a window has the 15 minutes it always
# had: short enough that a leaked link is rarely useful by the time it ends up
# somewhere it shouldn't.
URL_WINDOW = timedelta(minutes=30)
URL_LIFETIME = timedelta(minutes=45)


def _signing_window_start(now=None):
    now = now or datetime.now(timezone.utc)
    window = int(URL_WINDOW.total_seconds())
    return datetime.fromtimestamp(
        int(now.timestamp()) // window * window, tz=timezone.utc
    )


def get_presigned_url(object_name, response_headers=None):
    """Generates a temporary, secure URL using the PUBLIC client."""
    if not minio_client_public:
        raise Exception("Public Minio client not initialized.")

    try:
        url = minio_client_public.presigned_get_object(
            bucket_name=current_app.config["MINIO_BUCKET_NAME"],
            object_name=object_name,
            expires=URL_LIFETIME,
            response_headers=response_headers or {},
            request_date=_signing_window_start(),
        )
        return url
    except S3Error:
        current_app.logger.exception("Error generating presigned URL")
        return None


def delete_file(object_name):
    """Deletes a file from the configured Minio bucket using the INTERNAL client."""
    if not minio_client_internal:
        raise Exception("Internal Minio client not initialized.")

    try:
        minio_client_internal.remove_object(
            bucket_name=current_app.config["MINIO_BUCKET_NAME"], object_name=object_name
        )
        current_app.logger.info(f"Successfully deleted {object_name} from Minio.")
        return True
    except S3Error:
        current_app.logger.exception(f"Error deleting file {object_name} from Minio")
        return False
