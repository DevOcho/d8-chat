import os
from datetime import timedelta
from urllib.parse import urlparse

from flask import current_app
from minio import Minio
from minio.error import S3Error

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
    secure = app.config["MINIO_SECURE"]
    bucket_name = app.config["MINIO_BUCKET_NAME"]

    # 1. Configure the INTERNAL client for server-to-server communication
    internal_endpoint = app.config["MINIO_ENDPOINT"]
    minio_client_internal = Minio(
        internal_endpoint, access_key=access_key, secret_key=secret_key, secure=secure
    )

    # 2. Configure the PUBLIC client for generating user-facing URLs
    public_endpoint_url = app.config.get("MINIO_PUBLIC_URL")
    public_endpoint_host = internal_endpoint # Default to internal

    if public_endpoint_url:
        # The Minio client constructor requires just the 'hostname:port' part (netloc).
        # We parse the full URL from the config to extract it.
        parsed_url = urlparse(public_endpoint_url)
        if parsed_url.netloc:
            public_endpoint_host = parsed_url.netloc
        else:
            print(f"WARNING: MINIO_PUBLIC_URL ('{public_endpoint_url}') seems to be invalid. Falling back to internal endpoint for URL generation.")

    minio_client_public = Minio(
        public_endpoint_host, access_key=access_key, secret_key=secret_key, secure=secure
    )

    # Bucket existence check is an internal operation
    try:
        found = minio_client_internal.bucket_exists(bucket_name)
        if not found:
            minio_client_internal.make_bucket(bucket_name)
            print(f"Minio bucket '{bucket_name}' created.")
        else:
            print(f"Minio bucket '{bucket_name}' already exists.")
    except S3Error as exc:
        print("Error initializing Minio bucket:", exc)


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
    except S3Error as exc:
        print("Error uploading file to Minio:", exc)
        return False


def get_presigned_url(object_name):
    """Generates a temporary, secure URL using the PUBLIC client."""
    if not minio_client_public:
        raise Exception("Public Minio client not initialized.")

    try:
        # This now correctly generates the URL and signature using the public endpoint
        url = minio_client_public.presigned_get_object(
            bucket_name=current_app.config["MINIO_BUCKET_NAME"],
            object_name=object_name,
            expires=timedelta(hours=1),
        )
        return url
    except S3Error as exc:
        print("Error generating presigned URL:", exc)
        return None


def delete_file(object_name):
    """Deletes a file from the configured Minio bucket using the INTERNAL client."""
    if not minio_client_internal:
        raise Exception("Internal Minio client not initialized.")

    try:
        minio_client_internal.remove_object(
            bucket_name=current_app.config["MINIO_BUCKET_NAME"], object_name=object_name
        )
        print(f"Successfully deleted {object_name} from Minio.")
        return True
    except S3Error as exc:
        print(f"Error deleting file {object_name} from Minio:", exc)
        return False
