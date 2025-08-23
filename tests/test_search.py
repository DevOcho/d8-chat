# File: ./tests/test_search.py

import pytest
from app.models import (
    User,
    Channel,
    ChannelMember,
    Conversation,
    Message,
    WorkspaceMember,
    UserConversationStatus,
)

SEARCH_PAGE_SIZE = 20


@pytest.fixture
def search_setup(test_db, logged_in_client):
    """
    Creates a rich environment for testing search functionality.
    - user1 (logged in, from conftest)
    - user2, user3
    - A public channel, a private channel user1 can see, and a private channel user1 cannot see.
    - DMs between (user1, user2) and (user2, user3).
    - Messages with unique keywords scattered across these conversations.
    """
    user1 = User.get_by_id(1)
    user2 = User.create(
        id=2, username="user_two", email="two@example.com", display_name="Zelda Smith"
    )
    user3 = User.create(
        id=3,
        username="user_three",
        email="three@example.com",
        display_name="Link Jones",
    )

    workspace = WorkspaceMember.get(user=user1).workspace
    WorkspaceMember.create(user=user2, workspace=workspace)
    WorkspaceMember.create(user=user3, workspace=workspace)

    # --- Channels ---
    public_chan = Channel.create(workspace=workspace, name="public-searchable")
    ChannelMember.create(user=user1, channel=public_chan)
    ChannelMember.create(user=user2, channel=public_chan)

    private_chan_visible = Channel.create(
        workspace=workspace, name="private-visible", is_private=True
    )
    ChannelMember.create(user=user1, channel=private_chan_visible)

    private_chan_hidden = Channel.create(
        workspace=workspace, name="private-hidden", is_private=True
    )
    ChannelMember.create(user=user2, channel=private_chan_hidden)

    # --- Conversations ---
    pub_conv, _ = Conversation.get_or_create(
        conversation_id_str=f"channel_{public_chan.id}", type="channel"
    )
    priv_vis_conv, _ = Conversation.get_or_create(
        conversation_id_str=f"channel_{private_chan_visible.id}", type="channel"
    )
    dm_conv1, _ = Conversation.get_or_create(
        conversation_id_str=f"dm_{user1.id}_{user2.id}", type="dm"
    )
    dm_conv2, _ = Conversation.get_or_create(
        conversation_id_str=f"dm_{user2.id}_{user3.id}", type="dm"
    )  # Inaccessible to user1

    # --- Messages ---
    Message.create(user=user1, conversation=pub_conv, content="A message about apples.")
    Message.create(
        user=user2, conversation=priv_vis_conv, content="A message about carrots."
    )
    Message.create(user=user1, conversation=dm_conv1, content="A message about grapes.")
    Message.create(
        user=user2, conversation=dm_conv2, content="A secret message about oranges."
    )  # Inaccessible

    # <<< FIX: Create UserConversationStatus records to make DMs searchable >>>
    UserConversationStatus.create(user=user1, conversation=dm_conv1)
    UserConversationStatus.create(user=user2, conversation=dm_conv1)
    UserConversationStatus.create(user=user2, conversation=dm_conv2)
    UserConversationStatus.create(user=user3, conversation=dm_conv2)

    return {
        "user1": user1,
        "user2": user2,
        "user3": user3,
        "public_channel": public_chan,
        "private_visible": private_chan_visible,
        "private_hidden": private_chan_hidden,
    }


def test_search_for_accessible_messages(logged_in_client, search_setup):
    """
    WHEN searching for terms in messages the user has access to
    THEN the results should be found and have the correct context.
    """
    # Test searching for a message in a public channel
    res1 = logged_in_client.get("/chat/search?q=apples")
    assert res1.status_code == 200
    # FIX: The template uses curly quotes, so we check for that in the response.
    assert b"Search results for \xe2\x80\x9capples\xe2\x80\x9d" in res1.data
    assert b"# public-searchable" in res1.data  # Check context

    # Test searching for a message in a private channel the user is in
    res2 = logged_in_client.get("/chat/search?q=carrots")
    assert res2.status_code == 200
    assert b"# private-visible" in res2.data

    # Test searching for a message in a DM
    res3 = logged_in_client.get("/chat/search?q=grapes")
    assert res3.status_code == 200
    assert b"Zelda Smith" in res3.data  # DM partner's display name


def test_search_does_not_find_inaccessible_messages(logged_in_client, search_setup):
    """
    WHEN searching for a term in a message from a conversation the user is not part of
    THEN no results should be returned.
    """
    response = logged_in_client.get("/chat/search?q=oranges")
    assert response.status_code == 200
    assert b"No messages found matching your search." in response.data


def test_search_for_channels(logged_in_client, search_setup):
    """
    WHEN searching for channels
    THEN public channels and private channels the user is a member of should be found.
    """
    response = logged_in_client.get("/chat/search?q=searchable")
    assert response.status_code == 200
    assert (
        b'Channels <span class="badge bg-secondary rounded-pill">1</span>'
        in response.data
    )
    # This will be loaded via HTMX, so we check the paginated endpoint directly
    paginated_res = logged_in_client.get("/chat/search/channels?q=searchable")
    # FIX: Check for the highlighted HTML, not plain text.
    assert b"#public-<mark>searchable</mark>" in paginated_res.data

    response_private = logged_in_client.get("/chat/search?q=private-visible")
    assert (
        b'Channels <span class="badge bg-secondary rounded-pill">1</span>'
        in response_private.data
    )


def test_search_does_not_find_hidden_private_channels(logged_in_client, search_setup):
    """
    WHEN searching for a private channel the user is NOT a member of
    THEN it should not appear in the results.
    """
    response = logged_in_client.get("/chat/search?q=private-hidden")
    assert (
        b'Channels <span class="badge bg-secondary rounded-pill">0</span>'
        in response.data
    )


def test_search_for_users(logged_in_client, search_setup):
    """
    WHEN searching for users by username or display name
    THEN the correct users should be found.
    """
    # Search by display name
    res1 = logged_in_client.get("/chat/search?q=Zelda")
    assert b'People <span class="badge bg-secondary rounded-pill">1</span>' in res1.data
    paginated_res1 = logged_in_client.get("/chat/search/users?q=Zelda")
    assert b"<strong><mark>Zelda</mark> Smith</strong>" in paginated_res1.data
    assert b"user_two" in paginated_res1.data  # also show username

    # Search by username
    res2 = logged_in_client.get("/chat/search?q=user_three")
    assert b'People <span class="badge bg-secondary rounded-pill">1</span>' in res2.data
    paginated_res2 = logged_in_client.get("/chat/search/users?q=user_three")
    assert b"Link Jones" in paginated_res2.data


def test_empty_search_returns_nothing(logged_in_client):
    """
    WHEN an empty search query is submitted
    THEN the response should be an empty container, not an error.
    """
    response = logged_in_client.get("/chat/search?q=")
    assert response.status_code == 200
    assert response.data == b'<div id="search-results-content"></div>'


def test_search_messages_pagination(logged_in_client, search_setup):
    """
    GIVEN more messages than the page size
    WHEN a search is performed
    THEN the paginated endpoint should return the next page of results.
    """
    user1 = search_setup["user1"]
    pub_conv = Conversation.get(
        conversation_id_str=f"channel_{search_setup['public_channel'].id}"
    )
    # Create one more message than the page size to trigger pagination
    for i in range(SEARCH_PAGE_SIZE + 1):
        Message.create(
            user=user1, conversation=pub_conv, content=f"PAGINATION_TEST message {i}"
        )

    # Initial search
    res1 = logged_in_client.get("/chat/search?q=PAGINATION_TEST")
    # The test should check for the presence of the 'Load More' button, which indicates pagination.
    assert b'hx-get="/chat/search/messages?q=PAGINATION_TEST&amp;page=2"' in res1.data

    # Paginated search for page 2
    res2 = logged_in_client.get("/chat/search/messages?q=PAGINATION_TEST&page=2")
    assert res2.status_code == 200
    # The response will contain the highlighted search term.
    assert b"<mark>PAGINATION_TEST</mark> message 0" in res2.data
    # [THE FIX] On the last page, the 'Load More' button pointing to page 3 should NOT be present.
    # This is more specific and avoids the issue with the comment.
    assert (
        b'hx-get="/chat/search/messages?q=PAGINATION_TEST&amp;page=3"' not in res2.data
    )
