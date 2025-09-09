# tests/test_services.py

import io
import os
import pytest
from app.models import (
    User,
    Conversation,
    Channel,
    ChannelMember,
    WorkspaceMember,
    UploadedFile,
    Message,
    MessageAttachment,
)
from app.services import minio_service, chat_service
from minio.error import S3Error

# We use this to simulate a file upload in our tests
from werkzeug.datastructures import FileStorage


@pytest.fixture
def setup_channel_and_users_for_service(test_db):
    """
    A dedicated fixture for service-layer tests. Creates users and a conversation.
    """
    user1 = User.get_by_id(1)
    workspace = WorkspaceMember.get(user=user1).workspace
    channel = Channel.create(workspace=workspace, name="service-test-channel")
    conv, _ = Conversation.get_or_create(
        conversation_id_str=f"channel_{channel.id}", type="channel"
    )
    ChannelMember.create(user=user1, channel=channel)

    return {"sender": user1, "conversation": conv}


# --- Existing minio_service tests ---
def test_get_presigned_url(mocker):
    """
    GIVEN a mocked Minio client
    WHEN get_presigned_url is called with an object name
    THEN it should call the client's presigned_get_object method with the correct parameters.
    """
    # Arrange: Mock the global minio_client used by the service
    mock_client = mocker.patch("app.services.minio_service.minio_client")
    mock_client.presigned_get_object.return_value = "http://mock-url.com/file"

    # Act
    url = minio_service.get_presigned_url("my-object-name.jpg")

    # Assert
    assert url == "http://mock-url.com/file"
    mock_client.presigned_get_object.assert_called_once()
    # We can even inspect the arguments it was called with
    args, kwargs = mock_client.presigned_get_object.call_args
    assert kwargs["bucket_name"] == "d8chat"
    assert kwargs["object_name"] == "my-object-name.jpg"


def test_get_presigned_url_handles_s3error(mocker):
    """
    GIVEN a mocked Minio client that raises an S3Error
    WHEN get_presigned_url is called
    THEN it should catch the exception and return None.
    """
    # Arrange: Configure the mock to raise a correctly instantiated S3Error.
    mock_client = mocker.patch("app.services.minio_service.minio_client")
    mock_client.presigned_get_object.side_effect = S3Error(
        code="DummyCode",
        message="Test S3 Error",
        resource="dummy",
        request_id="dummy",
        host_id="dummy",
        response=None,
    )

    # Act
    url = minio_service.get_presigned_url("any-object.jpg")

    # Assert
    assert url is None


def test_delete_file_success(mocker):
    """
    GIVEN a mocked Minio client
    WHEN delete_file is called
    THEN it should call the client's remove_object method and return True.
    """
    # Arrange
    mock_client = mocker.patch("app.services.minio_service.minio_client")

    # Act
    result = minio_service.delete_file("file-to-delete.png")

    # Assert
    assert result is True
    mock_client.remove_object.assert_called_once_with(
        bucket_name="d8chat", object_name="file-to-delete.png"
    )


def test_delete_file_handles_s3error(mocker):
    """
    GIVEN a mocked Minio client that raises an S3Error
    WHEN delete_file is called
    THEN it should catch the exception and return False.
    """
    # Arrange
    mock_client = mocker.patch("app.services.minio_service.minio_client")
    mock_client.remove_object.side_effect = S3Error(
        code="DummyCode",
        message="Test S3 Delete Error",
        resource="dummy",
        request_id="dummy",
        host_id="dummy",
        response=None,
    )

    # Act
    result = minio_service.delete_file("a-file.txt")

    # Assert
    assert result is False


def test_upload_file_service_success(mocker):
    """
    GIVEN a valid file path and mocked dependencies
    WHEN minio_service.upload_file is called
    THEN it should call the Minio client's put_object and return True.
    """
    # Arrange
    mock_client = mocker.patch("app.services.minio_service.minio_client")
    mocker.patch("builtins.open", mocker.mock_open(read_data=b"test data"))
    mocker.patch("os.stat").return_value.st_size = 9  # Mock file size

    # Act
    result = minio_service.upload_file(
        object_name="new-file.txt",
        file_path="/fake/path/file.txt",
        content_type="text/plain",
    )

    # Assert
    assert result is True
    mock_client.put_object.assert_called_once()


def test_upload_file_service_handles_s3error(mocker):
    """
    GIVEN the Minio client will raise an error
    WHEN minio_service.upload_file is called
    THEN it should catch the error and return False.
    """
    # Arrange
    mock_client = mocker.patch("app.services.minio_service.minio_client")
    mock_client.put_object.side_effect = S3Error(
        code="DummyCode",
        message="Upload Failed",
        resource="d",
        request_id="r",
        host_id="h",
        response=None,
    )
    mocker.patch("builtins.open", mocker.mock_open(read_data=b"test data"))
    mocker.patch("os.stat").return_value.st_size = 9

    # Act
    result = minio_service.upload_file("fail.txt", "/fake/path/fail.txt", "text/plain")

    # Assert
    assert result is False


def test_handle_new_message_with_attachments(setup_channel_and_users_for_service):
    """
    GIVEN a user, conversation, and valid attachment IDs
    WHEN handle_new_message is called
    THEN it should create a new message AND associated MessageAttachment records.
    """
    # Arrange
    sender = setup_channel_and_users_for_service["sender"]
    conversation = setup_channel_and_users_for_service["conversation"]
    # Create dummy UploadedFile records to link to
    file1 = UploadedFile.create(
        uploader=sender,
        original_filename="f1.txt",
        stored_filename="s1.txt",
        mime_type="text/plain",
        file_size_bytes=1,
    )
    file2 = UploadedFile.create(
        uploader=sender,
        original_filename="f2.jpg",
        stored_filename="s2.jpg",
        mime_type="image/jpeg",
        file_size_bytes=1,
    )

    attachment_ids_str = f"{file1.id},{file2.id}"
    assert MessageAttachment.select().count() == 0

    # Act
    new_message = chat_service.handle_new_message(
        sender=sender,
        conversation=conversation,
        chat_text="Check out these files",
        attachment_file_ids=attachment_ids_str,
    )

    # Assert
    assert Message.select().count() == 1
    assert MessageAttachment.select().count() == 2

    # Verify that the links are correct
    attachments = MessageAttachment.select().where(
        MessageAttachment.message == new_message
    )
    attached_file_ids = {att.attachment_id for att in attachments}
    assert attached_file_ids == {file1.id, file2.id}
