import os
from datetime import timedelta

from flask import current_app
from minio import Minio
from minio.error import S3Error

minio_client = None


def init_app(app):
    """Initialize the Minio client and ensure the bucket exists."""
    global minio_client

    endpoint = app.config["MINIO_ENDPOINT"]
    access_key = app.config["MINIO_ACCESS_KEY"]
    secret_key = app.config["MINIO_SECRET_KEY"]
    secure = app.config["MINIO_SECURE"]
    bucket_name = app.config["MINIO_BUCKET_NAME"]

    minio_client = Minio(
        endpoint, access_key=access_key, secret_key=secret_key, secure=secure
    )

    try:
        found = minio_client.bucket_exists(bucket_name)
        if not found:
            minio_client.make_bucket(bucket_name)
            print(f"Minio bucket '{bucket_name}' created.")
        else:
            print(f"Minio bucket '{bucket_name}' already exists.")
    except S3Error as exc:
        print("Error initializing Minio bucket:", exc)


def upload_file(object_name, file_path, content_type):
    """Uploads a file to the configured Minio bucket."""
    if not minio_client:
        raise Exception("Minio client not initialized.")

    try:
        with open(file_path, "rb") as file_data:
            file_stat = os.stat(file_path)
            minio_client.put_object(
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
    """Generates a temporary, secure URL to access a file."""
    if not minio_client:
        raise Exception("Minio client not initialized.")

    try:
        # URL is valid for 1 hour
        url = minio_client.presigned_get_object(
            bucket_name=current_app.config["MINIO_BUCKET_NAME"],
            object_name=object_name,
            expires=timedelta(hours=1),
        )
        return url
    except S3Error as exc:
        print("Error generating presigned URL:", exc)
        return None


def delete_file(object_name):
    """Deletes a file from the configured Minio bucket."""
    if not minio_client:
        raise Exception("Minio client not initialized.")

    try:
        minio_client.remove_object(
            bucket_name=current_app.config["MINIO_BUCKET_NAME"], object_name=object_name
        )
        print(f"Successfully deleted {object_name} from Minio.")
        return True
    except S3Error as exc:
        print(f"Error deleting file {object_name} from Minio:", exc)
        return False
