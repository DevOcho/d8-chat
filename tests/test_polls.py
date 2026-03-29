# tests/test_polls.py
import pytest

from app.models import Channel, Conversation, Message, Poll, PollOption, User, Vote


@pytest.fixture
def setup_poll(test_db):
    """Fixture to set up a basic poll in the general channel."""
    user1 = User.get_by_id(1)
    channel = Channel.get(name="general")
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
