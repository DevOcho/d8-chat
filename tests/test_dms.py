# tests/test_dms.py

from app.models import User, Conversation, UserConversationStatus


def test_get_start_dm_form_lists_other_users(logged_in_client):
    """
    GIVEN two users exist in the database
    WHEN the "start DM" modal is requested by user 1
    THEN it should list the other user but not the logged-in user.
    """
    # --- THE FIX: Use a distinct username to prevent substring issues ---
    User.create(id=2, username="anotheruser", email="another@example.com")

    response = logged_in_client.get("/chat/dms/start")

    assert response.status_code == 200
    # Assert the distinct username is present
    assert b"anotheruser" in response.data
    # Assert the original username is NOT present. This will now work correctly.
    assert b"testuser" not in response.data


def test_open_dm_chat_with_user(logged_in_client):
    """
    GIVEN two users exist
    WHEN user 1 opens a DM chat with user 2 for the first time
    THEN a Conversation and two UserConversationStatus records should be created.
    """
    user1 = User.get_by_id(1)
    # --- THE FIX: Use a distinct username ---
    user2 = User.create(id=2, username="anotheruser", email="another@example.com")

    assert Conversation.select().where(Conversation.type == "dm").count() == 0

    response = logged_in_client.get(f"/chat/dm/{user2.id}")

    assert response.status_code == 200
    assert b'<div id="chat-header-container" hx-swap-oob="true">' in response.data
    # Assert the distinct username is present
    assert b"anotheruser" in response.data

    # Verify database records were created
    expected_conv_id_str = f"dm_{user1.id}_{user2.id}"
    conv = Conversation.get_or_none(conversation_id_str=expected_conv_id_str)
    assert conv is not None
    # ... (rest of the assertions are correct) ...
    assert conv.type == "dm"
    status1 = UserConversationStatus.get_or_none(user=user1, conversation=conv)
    status2 = UserConversationStatus.get_or_none(user=user2, conversation=conv)
    assert status1 is not None
    assert status2 is not None


def test_open_dm_with_nonexistent_user(logged_in_client):
    """
    Covers: `get_dm_chat` error handling for invalid user ID.
    WHEN a user tries to open a DM with a user ID that doesn't exist
    THEN the server should return a 404 Not Found error.
    """
    response = logged_in_client.get("/chat/dm/9999")
    assert response.status_code == 404
    assert b"User not found" in response.data


def test_leave_dm(logged_in_client):
    """
    Covers: `leave_dm` functionality.
    GIVEN a user is in a DM with another user
    WHEN they hit the leave_dm endpoint
    THEN they should be redirected and the UserConversationStatus should be deleted.
    """
    user2 = User.create(id=2, username="dm_partner", email="partner@example.com")
    # Simulate being in a DM by creating the conversation and status
    conv, _ = Conversation.get_or_create(conversation_id_str="dm_1_2", type="dm")
    UserConversationStatus.create(user_id=1, conversation=conv)

    assert UserConversationStatus.get_or_none(user_id=1, conversation=conv) is not None

    response = logged_in_client.delete(f"/chat/dm/{user2.id}/leave")

    # Successful leave should redirect to the main chat interface
    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == "/chat"
    # Verify the status record was deleted from the database
    assert UserConversationStatus.get_or_none(user_id=1, conversation=conv) is None
