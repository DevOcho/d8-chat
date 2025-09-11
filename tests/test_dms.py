# tests/test_dms.py
import pytest
from app.models import User, Conversation, UserConversationStatus, WorkspaceMember

# A smaller page size for the user search modal
DM_SEARCH_PAGE_SIZE = 20


@pytest.fixture
def setup_dm_search_users(test_db):
    """
    Creates a number of users to test searching and pagination for starting DMs.
    - user1 (testuser) is the logged-in user.
    - user2 (dm_partner) is already in a DM with user1.
    - 25 other users (search_user_00 to search_user_24) are available to be searched.
    """
    user1 = User.get_by_id(1)
    workspace = WorkspaceMember.get(user=user1).workspace

    # User already in a DM
    user2 = User.create(id=2, username="dm_partner", email="partner@example.com")
    WorkspaceMember.create(user=user2, workspace=workspace)
    conv, _ = Conversation.get_or_create(conversation_id_str="dm_1_2", type="dm")
    UserConversationStatus.create(user=user1, conversation=conv)

    # Create more users than will fit on one page
    for i in range(DM_SEARCH_PAGE_SIZE + 5):
        user = User.create(
            id=i + 3,
            # Zero-pad the number to ensure correct alphabetical sorting
            username=f"search_user_{i:02d}",
            email=f"search{i}@example.com",
            display_name=f"Search User {i:02d}",
        )
        WorkspaceMember.create(user=user, workspace=workspace)


def test_get_start_dm_form_lists_other_users(logged_in_client, setup_dm_search_users):
    """
    GIVEN multiple users exist
    WHEN the "start DM" modal is requested
    THEN it should list available users but not the logged-in user or existing DM partners.
    """
    response = logged_in_client.get("/chat/dms/start")

    assert response.status_code == 200
    # The logged-in user should not be in the list
    assert b"testuser" not in response.data
    # The user already in a DM should not be in the list
    assert b"dm_partner" not in response.data
    # The first searchable user (with padding) should be present
    assert b"search_user_00" in response.data
    # Check for the "Load More" button since we created more users than the page size
    assert b"Load More" in response.data


def test_search_users_for_dm(logged_in_client, setup_dm_search_users):
    """
    WHEN searching for users to start a DM
    THEN it should only return users matching the query.
    """
    # Search for a specific user that should only have one match
    response = logged_in_client.get("/chat/dms/search?q=search_user_15")

    assert response.status_code == 200
    assert b"search_user_15" in response.data
    # Check for a user that definitely does not match the query.
    assert b"search_user_07" not in response.data
    assert (
        b"Load More" not in response.data
    )  # Should not be a load more button for one result


def test_search_users_for_dm_pagination(logged_in_client, setup_dm_search_users):
    """
    WHEN searching returns more results than the page size
    THEN the "Load More" button should correctly fetch the next page.
    """
    # Request the second page of results for a broad query
    response = logged_in_client.get("/chat/dms/search?q=search_user&page=2")

    assert response.status_code == 200
    # The last user created should be on the second page
    assert b"search_user_24" in response.data
    # The first user should NOT be on the second page
    assert b"search_user_00" not in response.data
    # There are no more pages, so the button should not be present
    assert b"Load More" not in response.data


def test_open_dm_chat_with_user(logged_in_client):
    """
    GIVEN two users exist
    WHEN user 1 opens a DM chat with user 2 for the first time
    THEN a Conversation and two UserConversationStatus records should be created.
    """
    user1 = User.get_by_id(1)
    user2 = User.create(id=2, username="anotheruser", email="another@example.com")

    assert Conversation.select().where(Conversation.type == "dm").count() == 0

    response = logged_in_client.get(f"/chat/dm/{user2.id}")

    assert response.status_code == 200
    assert b'<div id="chat-header-container" hx-swap-oob="true">' in response.data
    assert b"anotheruser" in response.data

    # Verify database records were created
    expected_conv_id_str = f"dm_{user1.id}_{user2.id}"
    conv = Conversation.get_or_none(conversation_id_str=expected_conv_id_str)
    assert conv is not None
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
