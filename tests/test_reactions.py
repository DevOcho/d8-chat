import pytest
from app.models import User, Channel, ChannelMember, Conversation, Message, Reaction


@pytest.fixture
def setup_message(test_db):
    """A fixture that creates a channel with two users and a message from user1."""
    user1 = User.get_by_id(1)
    user2 = User.create(id=2, username="user_two", email="two@example.com")

    channel = Channel.create(workspace_id=1, name="reaction-channel")
    ChannelMember.create(user=user1, channel=channel)
    ChannelMember.create(user=user2, channel=channel)

    conv, _ = Conversation.get_or_create(
        conversation_id_str=f"channel_{channel.id}", type="channel"
    )

    message = Message.create(
        user=user1, conversation=conv, content="A message to react to"
    )
    return {"user1": user1, "user2": user2, "message": message, "channel": channel}


def test_add_reaction_success(logged_in_client, setup_message):
    """
    GIVEN a message
    WHEN a logged-in user posts a reaction to it
    THEN a new Reaction record should be created in the database.
    """
    message = setup_message["message"]
    emoji_char = "👍"

    assert Reaction.select().count() == 0

    response = logged_in_client.post(
        f"/chat/message/{message.id}/react", data={"emoji": emoji_char}
    )

    assert response.status_code == 200

    reaction = Reaction.get_or_none(message=message, user_id=1, emoji=emoji_char)
    assert reaction is not None
    assert Reaction.select().count() == 1


def test_toggle_reaction_removes_it(logged_in_client, setup_message):
    """
    GIVEN a message that the user has already reacted to
    WHEN the user posts the same reaction again
    THEN the Reaction record should be deleted.
    """
    message = setup_message["message"]
    emoji_char = "🎉"

    # First, add the reaction
    logged_in_client.post(
        f"/chat/message/{message.id}/react", data={"emoji": emoji_char}
    )
    assert Reaction.select().count() == 1

    # Now, "toggle" it by sending the same request again
    response = logged_in_client.post(
        f"/chat/message/{message.id}/react", data={"emoji": emoji_char}
    )

    assert response.status_code == 200
    assert Reaction.select().count() == 0


def test_loading_chat_shows_existing_reactions(logged_in_client, setup_message):
    """
    GIVEN a message with a reaction from another user
    WHEN the logged-in user loads that channel
    THEN the HTML should contain the reaction from the other user.
    """
    message = setup_message["message"]
    user2 = setup_message["user2"]
    channel = setup_message["channel"]
    emoji_char = "🚀"

    # User 2 adds a reaction
    Reaction.create(user=user2, message=message, emoji=emoji_char)

    # User 1 (the logged_in_client) loads the channel
    response = logged_in_client.get(f"/chat/channel/{channel.id}")

    assert response.status_code == 200
    # Check that the emoji is present in the response
    assert emoji_char.encode() in response.data
    # Check that the HTML contains the data attribute with user 2's ID
    assert b'data-reactor-ids="2"' in response.data
    # Check that the highlight class is NOT present, because user 1 didn't react
    assert b'class="btn btn-sm reaction-pill user-reacted"' not in response.data


def test_react_to_nonexistent_message(logged_in_client):
    """
    WHEN a user tries to react to a message ID that does not exist
    THEN they should receive a 400 Bad Request error.
    """
    response = logged_in_client.post("/chat/message/9999/react", data={"emoji": "🤔"})
    assert response.status_code == 400
    assert b"Invalid request" in response.data


def test_react_with_no_emoji(logged_in_client, setup_message):
    """
    WHEN a user sends a reaction request with no emoji data
    THEN they should receive a 400 Bad Request error.
    """
    message = setup_message["message"]
    response = logged_in_client.post(
        f"/chat/message/{message.id}/react", data={"emoji": ""}
    )
    assert response.status_code == 400
    assert b"Invalid request" in response.data
