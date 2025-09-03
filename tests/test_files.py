# tests/test_files.py

import io
import pytest
from app.models import UploadedFile

# We use this to simulate a file upload in our tests
from werkzeug.datastructures import FileStorage


def test_upload_file_success(logged_in_client, mocker):
    """
    GIVEN a logged-in user
    WHEN they upload a valid file
    THEN the file should be saved, a database record created, and a success response returned.
    """
    # Arrange:
    # 1. Mock the external Minio service so we don't actually upload anything.
    #    We tell the mock to return `True` to simulate a successful upload.
    minio_mock = mocker.patch(
        "app.blueprints.files.minio_service.upload_file", return_value=True
    )

    # 2. Create an in-memory "file" to upload.
    file_data = {"file": (io.BytesIO(b"this is a test file"), "test.png")}

    # Act: Post the file data to the upload endpoint.
    response = logged_in_client.post(
        "/files/upload", data=file_data, content_type="multipart/form-data"
    )

    # Assert:
    # 1. The response is successful (201 Created).
    assert response.status_code == 201
    assert "file_id" in response.json
    assert response.json["message"] == "File uploaded successfully"

    # 2. A record was created in our database.
    assert UploadedFile.select().count() == 1
    new_file = UploadedFile.get()
    assert new_file.original_filename == "test.png"
    assert new_file.uploader.id == 1  # The logged_in_client's user ID

    # 3. Our mock Minio service was called exactly once.
    minio_mock.assert_called_once()


def test_upload_no_file_part(logged_in_client):
    """
    WHEN a POST request is made without a 'file' part
    THEN the server should return a 400 Bad Request error.
    """
    response = logged_in_client.post("/files/upload", data={})
    assert response.status_code == 400
    assert response.json["error"] == "No file part"


def test_upload_empty_filename(logged_in_client):
    """
    WHEN a file is uploaded but has an empty filename
    THEN the server should return a 400 Bad Request error.
    """
    file_data = {"file": (io.BytesIO(b"some data"), "")}
    response = logged_in_client.post("/files/upload", data=file_data)
    assert response.status_code == 400
    assert response.json["error"] == "No selected file"


def test_upload_disallowed_extension(logged_in_client):
    """
    WHEN a file with a non-whitelisted extension (e.g., .exe) is uploaded
    THEN the server should return a 400 Bad Request error.
    """
    file_data = {"file": (io.BytesIO(b"malicious content"), "virus.exe")}
    response = logged_in_client.post(
        "/files/upload", data=file_data, content_type="multipart/form-data"
    )
    assert response.status_code == 400
    assert response.json["error"] == "File type not allowed"


def test_upload_file_too_large(logged_in_client, mocker):
    """
    WHEN a file is uploaded that exceeds the MAX_CONTENT_LENGTH
    THEN the server should return a 400 Bad Request error.
    """
    # Arrange:
    # 1. Temporarily reduce the allowed file size for this specific test.
    mocker.patch("app.blueprints.files.MAX_CONTENT_LENGTH", 10)

    # 2. Create a file that is larger than our new 10-byte limit.
    file_data = {
        "file": (
            io.BytesIO(b"this content is definitely more than 10 bytes"),
            "large.txt",
        )
    }

    # Act & Assert:
    response = logged_in_client.post(
        "/files/upload", data=file_data, content_type="multipart/form-data"
    )
    assert response.status_code == 400
    assert response.json["error"] == "File exceeds maximum size limit"


def test_upload_minio_failure(logged_in_client, mocker):
    """
    WHEN the file is valid but the Minio service fails to save it
    THEN the server should return a 500 Internal Server Error.
    """
    # Arrange:
    # 1. Mock the Minio service to simulate a failure by returning `False`.
    minio_mock = mocker.patch(
        "app.blueprints.files.minio_service.upload_file", return_value=False
    )

    # 2. Prepare a valid file.
    file_data = {"file": (io.BytesIO(b"this is a test file"), "test.pdf")}

    # Act & Assert:
    response = logged_in_client.post(
        "/files/upload", data=file_data, content_type="multipart/form-data"
    )
    assert response.status_code == 500
    assert response.json["error"] == "Failed to upload file to storage"

    # 3. Crucially, ensure no file record was created in our database.
    assert UploadedFile.select().count() == 0
