# tests/test_api_v1.py
import io

from app.models import User


def test_api_login_success(client):
    """
    GIVEN a user with a known password
    WHEN the API login endpoint is called with valid credentials
    THEN it should return a 200 response with an api_token and user data
    """
    # The 'testuser' (id=1) is created by conftest.py, but lacks a password. Let's set one.
    user = User.get_by_id(1)
    user.set_password("password123")
    user.save()

    response = client.post(
        "/api/v1/auth/login", json={"username": "testuser", "password": "password123"}
    )

    assert response.status_code == 200
    data = response.get_json()

    assert "api_token" in data
    assert data["api_token"].startswith("d8_sec_")
    assert "user" in data
    assert data["user"]["username"] == "testuser"


def test_api_login_failure(client):
    """
    WHEN the API login endpoint is called with invalid credentials
    THEN it should return a 401 Unauthorized response
    """
    response = client.post(
        "/api/v1/auth/login", json={"username": "testuser", "password": "wrongpassword"}
    )
    assert response.status_code == 401
    assert response.get_json()["error"] == "Invalid credentials"


def test_api_get_me_success(client):
    """
    GIVEN a valid api_token
    WHEN the /api/v1/auth/me endpoint is called with the token in the Authorization header
    THEN it should return the authenticated user's details
    """
    user = User.get_by_id(1)
    user.set_password("password123")
    user.save()

    # Login to get the token
    login_res = client.post(
        "/api/v1/auth/login", json={"username": "testuser", "password": "password123"}
    )
    token = login_res.get_json()["api_token"]

    # Use the token to access the protected route
    me_res = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert me_res.status_code == 200
    data = me_res.get_json()
    assert data["user"]["username"] == "testuser"


def test_api_get_me_unauthorized(client):
    """
    WHEN the /api/v1/auth/me endpoint is called without a token
    THEN it should return a 401 Unauthorized response
    """
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.get_json()["error"] == "Missing or invalid token"


def test_api_get_workspaces_success(client):
    """
    GIVEN a valid api_token
    WHEN the /api/v1/workspaces endpoint is called
    THEN it should return a list of workspaces the user is in
    """
    user = User.get_by_id(1)
    user.set_password("password123")
    user.save()

    login_res = client.post(
        "/api/v1/auth/login", json={"username": "testuser", "password": "password123"}
    )
    token = login_res.get_json()["api_token"]

    res = client.get("/api/v1/workspaces", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    data = res.get_json()

    assert "workspaces" in data
    assert len(data["workspaces"]) > 0
    assert data["workspaces"][0]["name"] == "DevOcho"  # Default from conftest


def test_api_get_channels_success(client):
    """
    GIVEN a valid api_token
    WHEN the /api/v1/channels endpoint is called
    THEN it should return the channels the user is a member of
    """
    from app.models import Channel, ChannelMember

    user = User.get_by_id(1)
    user.set_password("password123")
    user.save()

    # Explicitly add the testuser to the general channel so the list isn't empty
    channel = Channel.get(Channel.name == "general")
    ChannelMember.get_or_create(user=user, channel=channel)

    login_res = client.post(
        "/api/v1/auth/login", json={"username": "testuser", "password": "password123"}
    )
    token = login_res.get_json()["api_token"]

    res = client.get("/api/v1/channels", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    data = res.get_json()

    assert "channels" in data
    assert len(data["channels"]) > 0

    # Check that the unread counts are present in the response
    first_channel = data["channels"][0]
    assert "unread_count" in first_channel
    assert "mention_count" in first_channel


def test_api_get_dms_success(client):
    """
    GIVEN a valid api_token
    WHEN the /api/v1/dms endpoint is called
    THEN it should return the active DMs for the user
    """
    user = User.get_by_id(1)
    user.set_password("password123")
    user.save()

    login_res = client.post(
        "/api/v1/auth/login", json={"username": "testuser", "password": "password123"}
    )
    token = login_res.get_json()["api_token"]

    res = client.get("/api/v1/dms", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    data = res.get_json()

    assert "dms" in data
    # By default in conftest, testuser doesn't have any active DMs initialized
    assert isinstance(data["dms"], list)


def test_api_get_messages_success(client):
    """
    GIVEN a valid api_token and a conversation with messages
    WHEN the /api/v1/conversations/<conv_id>/messages endpoint is called
    THEN it should return a list of messages with attachments and reactions
    """
    from app.models import Channel, ChannelMember, Conversation, Message

    user = User.get_by_id(1)
    user.set_password("password123")
    user.save()

    # Ensure user is in the channel and create a test message
    channel = Channel.get(Channel.name == "general")
    ChannelMember.get_or_create(user=user, channel=channel)
    conv = Conversation.get(conversation_id_str=f"channel_{channel.id}")
    Message.create(user=user, conversation=conv, content="API Test Message")

    login_res = client.post(
        "/api/v1/auth/login", json={"username": "testuser", "password": "password123"}
    )
    token = login_res.get_json()["api_token"]

    res = client.get(
        f"/api/v1/conversations/{conv.conversation_id_str}/messages",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    data = res.get_json()

    assert "messages" in data
    assert len(data["messages"]) > 0
    # The most recent message should be at the end (chronological order)
    assert data["messages"][-1]["content"] == "API Test Message"
    assert "reactions" in data["messages"][-1]
    assert "attachments" in data["messages"][-1]


def test_api_get_thread_success(client):
    """
    GIVEN a parent message with thread replies
    WHEN the /api/v1/threads/<msg_id> endpoint is called
    THEN it should return the parent message and its replies
    """
    from app.models import Channel, ChannelMember, Conversation, Message

    user = User.get_by_id(1)

    # We need to set the password so the login works!
    user.set_password("password123")
    user.save()

    channel = Channel.get(Channel.name == "general")
    ChannelMember.get_or_create(user=user, channel=channel)
    conv = Conversation.get(conversation_id_str=f"channel_{channel.id}")

    parent = Message.create(
        user=user, conversation=conv, content="Parent Thread Message"
    )
    Message.create(
        user=user,
        conversation=conv,
        content="Thread Reply",
        parent_message=parent,
        reply_type="thread",
    )

    # Get token
    login_res = client.post(
        "/api/v1/auth/login", json={"username": "testuser", "password": "password123"}
    )
    token = login_res.get_json()["api_token"]

    res = client.get(
        f"/api/v1/threads/{parent.id}", headers={"Authorization": f"Bearer {token}"}
    )
    assert res.status_code == 200
    data = res.get_json()

    assert "parent_message" in data
    assert data["parent_message"]["content"] == "Parent Thread Message"
    assert "replies" in data
    assert len(data["replies"]) == 1
    assert data["replies"][0]["content"] == "Thread Reply"


def test_api_create_message_success_channel(client):
    """
    GIVEN a valid api_token and a conversation the user is in
    WHEN a POST request is made to create a message
    THEN it should return 201 and the serialized message data
    """
    from app.models import Channel, ChannelMember, Conversation

    user = User.get_by_id(1)
    user.set_password("password123")
    user.save()

    # Add user to general channel
    channel = Channel.get(Channel.name == "general")
    ChannelMember.get_or_create(user=user, channel=channel)
    conv = Conversation.get(conversation_id_str=f"channel_{channel.id}")

    login_res = client.post(
        "/api/v1/auth/login", json={"username": "testuser", "password": "password123"}
    )
    token = login_res.get_json()["api_token"]

    payload = {"content": "Hello API world!"}
    res = client.post(
        f"/api/v1/conversations/{conv.conversation_id_str}/messages",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 201
    data = res.get_json()
    assert data["content"] == "Hello API world!"
    assert data["user"]["username"] == "testuser"
    assert data["conversation_id_str"] == conv.conversation_id_str


def test_api_create_message_thread_reply(client):
    """
    GIVEN a valid api_token and a parent message
    WHEN a POST request is made to create a thread reply
    THEN it should return 201 and correctly link the parent message
    """
    from app.models import Channel, ChannelMember, Conversation, Message

    user = User.get_by_id(1)
    user.set_password("password123")
    user.save()

    channel = Channel.get(Channel.name == "general")
    ChannelMember.get_or_create(user=user, channel=channel)
    conv = Conversation.get(conversation_id_str=f"channel_{channel.id}")

    parent_msg = Message.create(user=user, conversation=conv, content="Parent")

    login_res = client.post(
        "/api/v1/auth/login", json={"username": "testuser", "password": "password123"}
    )
    token = login_res.get_json()["api_token"]

    payload = {
        "content": "Thread reply via API",
        "parent_message_id": parent_msg.id,
        "reply_type": "thread",
    }
    res = client.post(
        f"/api/v1/conversations/{conv.conversation_id_str}/messages",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 201
    data = res.get_json()
    assert data["content"] == "Thread reply via API"
    assert data["reply_type"] == "thread"
    assert data["parent_message_id"] == parent_msg.id


def test_api_create_message_missing_content(client):
    """
    WHEN POSTing without content
    THEN return 400 Bad Request
    """
    from app.models import Channel, ChannelMember, Conversation

    user = User.get_by_id(1)
    user.set_password("password123")
    user.save()

    channel = Channel.get(Channel.name == "general")
    ChannelMember.get_or_create(user=user, channel=channel)
    conv = Conversation.get(conversation_id_str=f"channel_{channel.id}")

    login_res = client.post(
        "/api/v1/auth/login", json={"username": "testuser", "password": "password123"}
    )
    token = login_res.get_json()["api_token"]

    # Post with empty content
    res = client.post(
        f"/api/v1/conversations/{conv.conversation_id_str}/messages",
        json={"content": ""},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 400
    assert res.get_json()["error"] == "Message content is required"


def test_api_create_message_access_denied(client):
    """
    GIVEN a valid api_token but a conversation the user is NOT in
    WHEN a POST request is made
    THEN it should return 403 Forbidden
    """
    from app.models import Channel, Conversation

    user = User.get_by_id(1)
    user.set_password("password123")
    user.save()

    # Create a private channel but DO NOT add the user to it
    channel = Channel.create(workspace_id=1, name="secret-api-channel", is_private=True)
    conv, _ = Conversation.get_or_create(
        conversation_id_str=f"channel_{channel.id}", defaults={"type": "channel"}
    )

    login_res = client.post(
        "/api/v1/auth/login", json={"username": "testuser", "password": "password123"}
    )
    token = login_res.get_json()["api_token"]

    payload = {"content": "Sneaking in"}
    res = client.post(
        f"/api/v1/conversations/{conv.conversation_id_str}/messages",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 403
    assert res.get_json()["error"] == "Access denied"


def test_api_upload_file_success(client, mocker):
    """
    GIVEN a valid api_token
    WHEN a valid file is posted to /api/v1/files/upload
    THEN it should upload the file and return a 201 with file details
    """
    user = User.get_by_id(1)
    user.set_password("password123")
    user.save()

    login_res = client.post(
        "/api/v1/auth/login", json={"username": "testuser", "password": "password123"}
    )
    token = login_res.get_json()["api_token"]

    # Mock the Minio service so we don't actually hit an external server
    mocker.patch("app.blueprints.api_v1.minio_service.upload_file", return_value=True)

    # Create an in-memory file
    file_data = {"file": (io.BytesIO(b"dummy image data"), "test_image.png")}

    res = client.post(
        "/api/v1/files/upload",
        data=file_data,
        content_type="multipart/form-data",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 201
    data = res.get_json()
    assert "file_id" in data
    assert data["message"] == "File uploaded successfully"
    assert data["original_filename"] == "test_image.png"
    assert data["mime_type"] == "image/png"
    assert "url" in data


def test_api_upload_file_missing_file(client):
    """
    WHEN POSTing to the upload endpoint without a file part
    THEN it should return 400 Bad Request
    """
    user = User.get_by_id(1)
    user.set_password("password123")
    user.save()

    login_res = client.post(
        "/api/v1/auth/login", json={"username": "testuser", "password": "password123"}
    )
    token = login_res.get_json()["api_token"]

    res = client.post(
        "/api/v1/files/upload",
        data={},
        content_type="multipart/form-data",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 400
    assert res.get_json()["error"] == "No file part"
