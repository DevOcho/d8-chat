# tests/test_polls.py
import pytest

from app.models import (
    Channel,
    ChannelMember,
    Conversation,
    Message,
    Poll,
    PollOption,
    User,
    Vote,
)


@pytest.fixture
def setup_poll(test_db):
    """Fixture to set up a basic poll in the general channel."""
    user1 = User.get_by_id(1)
    channel = Channel.get(name="general")
    # Creating and voting both require membership of the poll's conversation.
    ChannelMember.get_or_create(user=user1, channel=channel)
    conv = Conversation.get(conversation_id_str=f"channel_{channel.id}")

    # Create a poll message
    poll_msg = Message.create(
        user=user1, conversation=conv, content="[Poll]: Best color?"
    )
    poll = Poll.create(message=poll_msg, question="Best color?")
    opt1 = PollOption.create(poll=poll, text="Red")
    opt2 = PollOption.create(poll=poll, text="Blue")

    return {"user1": user1, "conv": conv, "poll": poll, "opt1": opt1, "opt2": opt2}


def test_get_create_poll_form(logged_in_client):
    """Test that the poll creation form loads correctly."""
    response = logged_in_client.get("/chat/poll/create_form")
    assert response.status_code == 200
    assert b"Create a Poll" in response.data


def test_create_poll_success(logged_in_client, setup_poll):
    """Test successfully creating a new poll."""
    conv = setup_poll["conv"]

    response = logged_in_client.post(
        "/chat/poll/create",
        data={
            "conversation_id_str": conv.conversation_id_str,
            "question": "Tabs or Spaces?",
            "options[]": ["Tabs", "Spaces"],
        },
    )

    assert response.status_code == 200
    assert "close-modal" in response.headers.get("HX-Trigger", "")
    assert Poll.select().where(Poll.question == "Tabs or Spaces?").count() == 1


def test_create_poll_validation_fails(logged_in_client, setup_poll):
    """Test that creating a poll with fewer than 2 options fails validation."""
    conv = setup_poll["conv"]

    response = logged_in_client.post(
        "/chat/poll/create",
        data={
            "conversation_id_str": conv.conversation_id_str,
            "question": "Only one option?",
            "options[]": ["Option 1"],
        },
    )

    assert response.status_code == 200
    assert b"at least two options are required" in response.data
    assert Poll.select().where(Poll.question == "Only one option?").count() == 0


def test_create_poll_form_starts_with_two_blank_options(logged_in_client):
    html = logged_in_client.get("/chat/poll/create_form").get_data(as_text=True)
    container = html.split('id="poll-options-container"', 1)[1].split("<template", 1)[0]
    assert container.count('name="options[]"') == 2
    assert 'placeholder="Option 2"' in container
    assert "<script" not in html


def test_failed_poll_keeps_what_was_typed_escaped(logged_in_client, setup_poll):
    """The options come back as rendered rows, escaped (they used to be
    re-inserted by an inline script through innerHTML)."""
    response = logged_in_client.post(
        "/chat/poll/create",
        data={
            "conversation_id_str": setup_poll["conv"].conversation_id_str,
            "question": "",
            "options[]": ['"><img src=x onerror=alert(1)>', "Pizza"],
        },
    )
    html = response.get_data(as_text=True)
    assert "<img src=x" not in html
    assert 'value="&#34;&gt;&lt;img src=x onerror=alert(1)&gt;"' in html
    assert 'value="Pizza"' in html


def test_create_poll_missing_conversation(logged_in_client):
    """Test that creating a poll without a conversation ID fails."""
    response = logged_in_client.post(
        "/chat/poll/create",
        data={"question": "Tabs or Spaces?", "options[]": ["Tabs", "Spaces"]},
    )
    assert response.status_code == 400
    assert b"Could not determine the current conversation" in response.data


def test_vote_on_poll(logged_in_client, setup_poll):
    """Test voting, switching a vote, and removing a vote."""
    opt1 = setup_poll["opt1"]
    opt2 = setup_poll["opt2"]

    # 1. Vote for option 1
    res1 = logged_in_client.post(f"/chat/poll/option/{opt1.id}/vote")
    assert res1.status_code == 200
    assert Vote.select().where(Vote.option == opt1).count() == 1
    assert b"1 vote<" in res1.data and b"1 votes" not in res1.data

    # 2. Switch vote to option 2
    res2 = logged_in_client.post(f"/chat/poll/option/{opt2.id}/vote")
    assert res2.status_code == 200
    assert Vote.select().where(Vote.option == opt1).count() == 0
    assert Vote.select().where(Vote.option == opt2).count() == 1

    # 3. Un-vote (clicking the same option again removes the vote)
    res3 = logged_in_client.post(f"/chat/poll/option/{opt2.id}/vote")
    assert res3.status_code == 200
    assert Vote.select().where(Vote.option == opt2).count() == 0


def test_vote_on_nonexistent_option(logged_in_client):
    """Test that voting on an invalid option returns 404."""
    response = logged_in_client.post("/chat/poll/option/9999/vote")
    assert response.status_code == 404
    assert b"Poll option not found" in response.data


# --- What a vote announces (web and API alike) ---------------------------------


def _published_api_data(channel_prefix="chat:"):
    import json

    from app.chat_manager import chat_manager

    frames = []
    for call in chat_manager.redis_client.publish.call_args_list:
        channel, payload = call.args
        data = json.loads(payload)
        if channel.startswith(channel_prefix) and "api_data" in data:
            frames.append(data["api_data"])
    return frames


def test_web_vote_tells_mobile_with_message_edited(logged_in_client, setup_poll):
    """Mobile listens for message_edited (what an API vote sends); a web vote
    used to send poll_update, which the app ignored."""
    poll, opt1 = setup_poll["poll"], setup_poll["opt1"]
    logged_in_client.post(f"/chat/poll/option/{opt1.id}/vote")

    (frame,) = _published_api_data()
    assert frame["type"] == "message_edited"
    data = frame["data"]
    assert data["id"] == poll.message.id
    assert data["poll"]["id"] == poll.id
    counts = {o["id"]: o["count"] for o in data["poll"]["options"]}
    assert counts == {opt1.id: 1, setup_poll["opt2"].id: 0}
    # The voter's choice isn't everyone's.
    assert "voted_option_id" not in data["poll"]


def test_api_vote_frame_leaves_out_the_voters_choice(client, setup_poll):
    from app.blueprints.api_v1 import generate_api_token

    poll, opt2 = setup_poll["poll"], setup_poll["opt2"]
    token = generate_api_token(setup_poll["user1"].id)
    res = client.post(
        f"/api/v1/polls/{poll.id}/vote",
        json={"option_id": opt2.id},
        headers={"Authorization": f"Bearer {token}"},
    )
    # The voter's own response still says what they chose...
    assert res.get_json()["poll"]["voted_option_id"] == opt2.id
    # ...the frame everyone else gets doesn't.
    (frame,) = _published_api_data()
    assert frame["type"] == "message_edited"
    assert "voted_option_id" not in frame["data"]["poll"]
    assert {o["id"]: o["count"] for o in frame["data"]["poll"]["options"]}[opt2.id] == 1


def test_web_and_api_votes_share_the_toggle(logged_in_client, setup_poll):
    """Same option twice withdraws, another option switches (cast_vote)."""
    from app.blueprints.polls import cast_vote

    user, poll = setup_poll["user1"], setup_poll["poll"]
    opt1, opt2 = setup_poll["opt1"], setup_poll["opt2"]
    cast_vote(user, poll, opt1)
    cast_vote(user, poll, opt2)
    assert [v.option_id for v in Vote.select()] == [opt2.id]
    cast_vote(user, poll, opt2)
    assert Vote.select().count() == 0
