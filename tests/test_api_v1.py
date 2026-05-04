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

    # Create an in-memory file — real PNG bytes so the content sniffer accepts
    # it; the upload pipeline now rejects bytes that don't match the extension.
    tiny_png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
        b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    file_data = {"file": (io.BytesIO(tiny_png), "test_image.png")}

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


def test_api_upload_file_too_large(client, mocker):
    """
    Files larger than MAX_CONTENT_LENGTH are rejected before they reach
    Minio. The test forces the limit down to a few bytes so we don't have
    to upload 50MB of fake content.
    """
    user = User.get_by_id(1)
    user.set_password("password123")
    user.save()
    login_res = client.post(
        "/api/v1/auth/login", json={"username": "testuser", "password": "password123"}
    )
    token = login_res.get_json()["api_token"]

    upload_mock = mocker.patch(
        "app.blueprints.api_v1.minio_service.upload_file", return_value=True
    )
    mocker.patch("app.blueprints.api_v1.MAX_CONTENT_LENGTH", 16)

    # Real PNG bytes — content sniff passes, then size check fails.
    tiny_png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
        b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    res = client.post(
        "/api/v1/files/upload",
        data={"file": (io.BytesIO(tiny_png), "big.png")},
        content_type="multipart/form-data",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 400
    assert "exceeds" in res.get_json()["error"]
    upload_mock.assert_not_called()


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


def test_api_get_file_content_success(client, mocker):
    """
    GIVEN a valid api_token and an existing file
    WHEN a GET request is made to /api/v1/files/<file_id>/content
    THEN it should return a 200 OK and stream the file content
    """
    from app.models import UploadedFile

    user = User.get_by_id(1)
    user.set_password("password123")
    user.save()

    login_res = client.post(
        "/api/v1/auth/login", json={"username": "testuser", "password": "password123"}
    )
    token = login_res.get_json()["api_token"]

    dummy_file = UploadedFile.create(
        uploader=user,
        original_filename="test.txt",
        stored_filename="dummy-uuid.txt",
        mime_type="text/plain",
        file_size_bytes=12,
    )

    # Create a mock response object that mimics urllib3 HTTPResponse
    mock_response = mocker.Mock()
    mock_response.stream.return_value = [b"mock ", b"file ", b"content"]

    # Mock the internal Minio client
    mocker.patch(
        "app.blueprints.api_v1.minio_service.minio_client_internal.get_object",
        return_value=mock_response,
    )

    res = client.get(
        f"/api/v1/files/{dummy_file.id}/content",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 200
    assert res.mimetype == "text/plain"
    assert res.headers["Cache-Control"] == "private, max-age=3600"
    assert "test.txt" in res.headers["Content-Disposition"]
    assert res.data == b"mock file content"

    # Ensure connection cleanup was called
    mock_response.close.assert_called_once()
    mock_response.release_conn.assert_called_once()


def test_api_get_file_content_not_found(client):
    """
    WHEN requesting a file that doesn't exist
    THEN return 404 Not Found
    """
    user = User.get_by_id(1)
    user.set_password("password123")
    user.save()

    login_res = client.post(
        "/api/v1/auth/login", json={"username": "testuser", "password": "password123"}
    )
    token = login_res.get_json()["api_token"]

    res = client.get(
        "/api/v1/files/9999/content", headers={"Authorization": f"Bearer {token}"}
    )

    assert res.status_code == 404
    assert res.get_json()["error"] == "File not found"


def test_api_get_conversation_members(client):
    """
    GIVEN a valid token and a conversation ID
    WHEN a GET request is made to the members endpoint
    THEN it should return the list of users in that conversation
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

    res = client.get(
        f"/api/v1/conversations/{conv.conversation_id_str}/members",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 200
    data = res.get_json()
    assert "members" in data
    assert any(member["username"] == "testuser" for member in data["members"])


def test_api_create_poll_and_vote(client):
    """
    GIVEN a valid token
    WHEN a poll is created and voted on via the API
    THEN it returns the proper payload and records the vote
    """
    from app.models import Channel, ChannelMember, Conversation, Vote

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

    # 1. Create the Poll
    poll_payload = {"question": "Best IDE?", "options": ["VSCode", "Vim"]}
    res1 = client.post(
        f"/api/v1/conversations/{conv.conversation_id_str}/polls",
        json=poll_payload,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res1.status_code == 201
    data = res1.get_json()
    assert "poll" in data
    assert data["poll"]["question"] == "Best IDE?"
    assert len(data["poll"]["options"]) == 2
    assert data["poll"]["voted_option_id"] is None

    poll_id = data["poll"]["id"]
    option_id = data["poll"]["options"][0]["id"]

    # 2. Vote on the Poll
    res2 = client.post(
        f"/api/v1/polls/{poll_id}/vote",
        json={"option_id": option_id},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res2.status_code == 200
    vote_data = res2.get_json()
    assert vote_data["poll"]["voted_option_id"] == option_id
    assert vote_data["poll"]["options"][0]["count"] == 1
    assert Vote.select().count() == 1


def test_api_search_success(client):
    """
    GIVEN a valid query
    WHEN the search API is called
    THEN it returns matching messages, channels, and people
    """
    from app.models import Channel, ChannelMember, Conversation, Message

    user = User.get_by_id(1)
    user.set_password("password123")
    user.save()

    channel = Channel.get(Channel.name == "general")
    ChannelMember.get_or_create(user=user, channel=channel)
    conv = Conversation.get(conversation_id_str=f"channel_{channel.id}")
    Message.create(user=user, conversation=conv, content="Searching for Apollo keyword")

    login_res = client.post(
        "/api/v1/auth/login", json={"username": "testuser", "password": "password123"}
    )
    token = login_res.get_json()["api_token"]

    res = client.get(
        "/api/v1/search?q=Apollo", headers={"Authorization": f"Bearer {token}"}
    )
    assert res.status_code == 200

    data = res.get_json()
    assert data["query"] == "Apollo"
    assert len(data["messages"]) > 0
    assert data["messages"][0]["content"] == "Searching for Apollo keyword"
    assert data["messages"][0]["conversation_name"] == "general"

    assert "channels" in data
    assert "people" in data


def test_api_get_messages_around_id(client):
    """
    GIVEN a conversation with multiple messages
    WHEN calling get_messages with around_message_id
    THEN it returns messages centered on that ID chronologically
    """
    from app.models import Channel, ChannelMember, Conversation, Message

    user = User.get_by_id(1)
    user.set_password("password123")
    user.save()

    channel = Channel.get(Channel.name == "general")
    ChannelMember.get_or_create(user=user, channel=channel)
    conv = Conversation.get(conversation_id_str=f"channel_{channel.id}")

    Message.create(user=user, conversation=conv, content="First")
    msg2 = Message.create(user=user, conversation=conv, content="Target")
    Message.create(user=user, conversation=conv, content="Last")

    login_res = client.post(
        "/api/v1/auth/login", json={"username": "testuser", "password": "password123"}
    )
    token = login_res.get_json()["api_token"]

    res = client.get(
        f"/api/v1/conversations/{conv.conversation_id_str}/messages?around_message_id={msg2.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 200
    data = res.get_json()

    contents = list((m["content"] for m in data["messages"]))
    assert "First" in contents
    assert "Target" in contents
    assert "Last" in contents


def test_api_app_config(client, mocker):
    """
    WHEN the /api/v1/app-config endpoint is hit
    THEN it should return the server branding and SSO flags
    """
    # Mock the external Authlib call to prevent real HTTP requests during testing
    mocker.patch(
        "app.blueprints.api_v1.oauth.authentik.create_authorization_url",
        return_value=("https://mock-sso-url.com/auth", "mock_state"),
    )

    res = client.get("/api/v1/app-config")

    assert res.status_code == 200
    data = res.get_json()
    assert data["server_name"] == "DevOcho"
    assert data["primary_color"] == "#ec729c"
    assert data["password_auth_enabled"] is True
    assert "sso_enabled" in data
    # Optional: verify the mocked URL made it through
    if data["sso_enabled"]:
        assert data["sso_auth_url"] == "https://mock-sso-url.com/auth"


def test_api_app_config_sso_unreachable(client, mocker):
    """
    WHEN the OIDC provider is unreachable (e.g. DNS failure in dev)
    THEN the endpoint should still return 200 with sso_auth_url: null
    """
    mocker.patch(
        "app.blueprints.api_v1.oauth.authentik.create_authorization_url",
        side_effect=Exception("DNS resolution failed: authentik.devocho.com"),
    )
    mocker.patch.dict(
        "app.blueprints.api_v1.current_app.config",
        {"OIDC_CLIENT_ID": "test-client-id"},
    )

    res = client.get("/api/v1/app-config")

    assert res.status_code == 200
    data = res.get_json()
    assert data["sso_auth_url"] is None


def test_api_sso_exchange_success(client, mocker):
    """
    GIVEN an authorization code from an OIDC provider
    WHEN it is posted to the /auth/sso/exchange endpoint
    THEN it exchanges the code for a token and creates/returns the user
    """
    fake_user_info = {
        "sub": "fake_sso_id_mobile",
        "email": "mobile.user@example.com",
        "given_name": "Mobile User",
    }
    mocker.patch(
        "app.blueprints.api_v1.oauth.authentik.fetch_access_token",
        return_value={"access_token": "fake_oauth_token"},
    )
    mocker.patch(
        "app.blueprints.api_v1.oauth.authentik.parse_id_token",
        return_value=fake_user_info,
    )

    payload = {"code": "auth_code_123", "redirect_uri": "d8chat://auth/callback"}
    res = client.post("/api/v1/auth/sso/exchange", json=payload)

    assert res.status_code == 200
    data = res.get_json()
    assert "api_token" in data
    assert data["user"]["email"] == "mobile.user@example.com"
    assert data["user"]["display_name"] == "Mobile User"


def test_api_update_me(client):
    """
    GIVEN a valid token
    WHEN a PATCH is made to /api/v1/users/me
    THEN it updates the user's details
    """
    user = User.get_by_id(1)
    user.set_password("password123")
    user.save()

    login_res = client.post(
        "/api/v1/auth/login", json={"username": "testuser", "password": "password123"}
    )
    token = login_res.get_json()["api_token"]

    res = client.patch(
        "/api/v1/users/me",
        json={"display_name": "Updated Name From API"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 200
    assert res.get_json()["display_name"] == "Updated Name From API"

    updated_user = User.get_by_id(1)
    assert updated_user.display_name == "Updated Name From API"


def test_rate_limit_returns_json(app):
    """
    GIVEN rate limiting is enabled
    WHEN a client exceeds the login rate limit
    THEN the 429 response should be JSON, not HTML
    """
    from app import limiter

    app.config["RATELIMIT_ENABLED"] = True
    app.config["RATELIMIT_STORAGE_URI"] = "memory://"
    limiter.init_app(app)

    with app.test_client() as client:
        for _ in range(10):
            client.post("/api/v1/auth/login", json={"username": "x", "password": "y"})

        res = client.post("/api/v1/auth/login", json={"username": "x", "password": "y"})

    app.config["RATELIMIT_ENABLED"] = False

    assert res.status_code == 429
    data = res.get_json()
    assert data is not None, "429 response must be JSON, not HTML"
    assert data["error"] == "Rate limit exceeded"
    assert "detail" in data


def test_api_mark_conversation_read(client, mocker):
    """
    GIVEN a valid token and a conversation with unread messages
    WHEN POST /api/v1/conversations/<conv_id>/read is called
    THEN it returns 204, updates last_read_timestamp, and broadcasts unread_updated
    """
    from app.models import (
        Channel,
        ChannelMember,
        Conversation,
        Message,
        UserConversationStatus,
    )

    user = User.get_by_id(1)
    user.set_password("password123")
    user.save()

    channel = Channel.get(Channel.name == "general")
    ChannelMember.get_or_create(user=user, channel=channel)
    conv = Conversation.get(conversation_id_str=f"channel_{channel.id}")
    Message.create(user=user, conversation=conv, content="Unread message")

    mock_send = mocker.patch("app.chat_manager.chat_manager.send_to_user")

    login_res = client.post(
        "/api/v1/auth/login", json={"username": "testuser", "password": "password123"}
    )
    token = login_res.get_json()["api_token"]

    res = client.post(
        f"/api/v1/conversations/{conv.conversation_id_str}/read",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 204
    assert res.data == b""

    status = UserConversationStatus.get(user=user, conversation=conv)
    assert status.last_read_timestamp is not None

    mock_send.assert_called_once()
    call_args = mock_send.call_args
    assert call_args[0][0] == user.id
    api_data = call_args[0][1]["api_data"]
    assert api_data["type"] == "unread_updated"
    assert api_data["data"]["conversation_id_str"] == conv.conversation_id_str
    assert api_data["data"]["unread_count"] == 0
    assert api_data["data"]["is_mention"] is False


def test_api_mark_conversation_read_non_member(client):
    """
    GIVEN a valid token but a channel the user is NOT in
    WHEN POST /api/v1/conversations/<conv_id>/read is called
    THEN it returns 403
    """
    from app.models import Channel, Conversation

    user = User.get_by_id(1)
    user.set_password("password123")
    user.save()

    channel = Channel.create(workspace_id=1, name="private-read-test", is_private=True)
    conv, _ = Conversation.get_or_create(
        conversation_id_str=f"channel_{channel.id}", defaults={"type": "channel"}
    )

    login_res = client.post(
        "/api/v1/auth/login", json={"username": "testuser", "password": "password123"}
    )
    token = login_res.get_json()["api_token"]

    res = client.post(
        f"/api/v1/conversations/{conv.conversation_id_str}/read",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 403


def test_api_update_presence(client):
    """
    GIVEN a valid token
    WHEN a POST is made to /api/v1/users/me/presence
    THEN it updates the user's presence status
    """
    user = User.get_by_id(1)
    user.set_password("password123")
    user.save()

    login_res = client.post(
        "/api/v1/auth/login", json={"username": "testuser", "password": "password123"}
    )
    token = login_res.get_json()["api_token"]

    res = client.post(
        "/api/v1/users/me/presence",
        json={"status": "busy"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 200
    assert res.get_json()["status"] == "busy"

    updated_user = User.get_by_id(1)
    assert updated_user.presence_status == "busy"


def _seed_helpdesk(workspace=None):
    """Create the helpdesk-bot user and #helpdesk channel for internal-notify tests."""
    from app.models import (
        Channel,
        ChannelMember,
        Conversation,
        User,
        Workspace,
        WorkspaceMember,
    )

    if workspace is None:
        workspace = Workspace.get(Workspace.name == "DevOcho")
    bot, _ = User.get_or_create(
        username="helpdesk-bot",
        defaults={
            "email": "helpdesk-bot@d8chat.local",
            "display_name": "Helpdesk Bot",
            "is_active": False,
        },
    )
    WorkspaceMember.get_or_create(user=bot, workspace=workspace)
    channel, _ = Channel.get_or_create(workspace=workspace, name="helpdesk")
    Conversation.get_or_create(
        conversation_id_str=f"channel_{channel.id}",
        defaults={"type": "channel"},
    )
    ChannelMember.get_or_create(user=bot, channel=channel)
    return bot, channel


def test_internal_notify_success(client):
    """
    GIVEN the correct shared secret and a known channel
    WHEN POSTing to /api/v1/internal/notify
    THEN it should return 200, persist the message, and author it as helpdesk-bot
    """
    from app.models import Conversation, Message

    bot, channel = _seed_helpdesk()

    res = client.post(
        "/api/v1/internal/notify",
        json={
            "channel_name": "helpdesk",
            "message": "[NEW] Ticket #42 — ACME Corp: 'Login button broken'",
        },
        headers={"X-Internal-Key": "test-internal-notify-key"},
    )

    assert res.status_code == 200
    assert res.get_json() == {"ok": True}

    conv = Conversation.get(conversation_id_str=f"channel_{channel.id}")
    msg = Message.get(Message.conversation == conv)
    assert msg.user_id == bot.id
    assert "Ticket #42" in msg.content


def test_internal_notify_bad_key(client):
    """A wrong/missing X-Internal-Key returns 401 and creates no message."""
    from app.models import Conversation, Message

    _bot, channel = _seed_helpdesk()

    res = client.post(
        "/api/v1/internal/notify",
        json={"channel_name": "helpdesk", "message": "should not post"},
        headers={"X-Internal-Key": "wrong-key"},
    )
    assert res.status_code == 401

    res_missing = client.post(
        "/api/v1/internal/notify",
        json={"channel_name": "helpdesk", "message": "should not post"},
    )
    assert res_missing.status_code == 401

    conv = Conversation.get(conversation_id_str=f"channel_{channel.id}")
    assert Message.select().where(Message.conversation == conv).count() == 0


def test_internal_notify_unknown_channel(client):
    """An unknown channel_name returns 404."""
    _seed_helpdesk()
    res = client.post(
        "/api/v1/internal/notify",
        json={"channel_name": "does-not-exist", "message": "hi"},
        headers={"X-Internal-Key": "test-internal-notify-key"},
    )
    assert res.status_code == 404


def test_internal_notify_bad_payload(client):
    """Missing channel_name or message returns 400."""
    _seed_helpdesk()

    res_no_msg = client.post(
        "/api/v1/internal/notify",
        json={"channel_name": "helpdesk"},
        headers={"X-Internal-Key": "test-internal-notify-key"},
    )
    assert res_no_msg.status_code == 400

    res_no_channel = client.post(
        "/api/v1/internal/notify",
        json={"message": "hi"},
        headers={"X-Internal-Key": "test-internal-notify-key"},
    )
    assert res_no_channel.status_code == 400
